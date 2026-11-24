"""Shared helpers used by every ``calendar_*`` tool.

Works out whose calendars a question is about (the speaker, a named
family member, or an email address) and which calendar ids belong to
that person, plus the ISO parsing the tools share.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .... import models
from ....integrations import google_oauth
from .._registry import ToolContext, ToolError


def looks_like_email(value: str) -> bool:
    """Lightweight email heuristic — good enough to branch the resolver."""
    return "@" in value and "." in value.split("@", 1)[1]


# How people refer to themselves when the model passes their words
# straight through instead of a name.
_SELF_WORDS = frozenset(
    {"me", "myself", "i", "my", "mine", "self", "speaker", "user", "the user"}
)


def _name_parts(p: models.Person) -> set[str]:
    parts: set[str] = set()
    for value in (p.first_name, p.preferred_name, p.middle_name, p.last_name):
        parts.update(re.findall(r"[a-z0-9]+", (value or "").lower()))
    return parts


def _full_name(p: models.Person) -> str:
    return " ".join(x for x in (p.preferred_name or p.first_name, p.last_name) if x)


def find_family_member(ctx: ToolContext, name: str) -> Optional[models.Person]:
    """Find the one household member a name refers to.

    Tries the strictest reading first and stops at the first that
    matches anyone:

    1. the name is exactly their first or preferred name ("Sam" is Sam,
       not Samantha);
    2. every word is one of their names ("Benjamin Maisano", "Maisano");
    3. every word starts one of their names ("Ben" for Benjamin);
    4. the text appears somewhere in their full name.

    Returns ``None`` when nobody matches. Raises :class:`ToolError`
    when several people match equally well — answering about the wrong
    person's calendar is worse than asking which one was meant.
    """
    needle = re.sub(r"['’]s\b", "", (name or "").strip().lower())
    words = re.findall(r"[a-z0-9]+", needle)
    if not words:
        return None
    people = (
        ctx.db.query(models.Person)
        .filter(models.Person.family_id == ctx.family_id)
        .order_by(models.Person.person_id)
        .all()
    )
    joined = " ".join(words)

    def given_name(p: models.Person) -> bool:
        return joined in (
            (p.first_name or "").strip().lower(),
            (p.preferred_name or "").strip().lower(),
        )

    def whole_words(p: models.Person) -> bool:
        return all(w in _name_parts(p) for w in words)

    def word_starts(p: models.Person) -> bool:
        parts = _name_parts(p)
        return all(any(part.startswith(w) for part in parts) for w in words)

    def anywhere(p: models.Person) -> bool:
        haystack = " ".join(
            x for x in (p.first_name, p.preferred_name, p.last_name) if x
        ).lower()
        return joined in haystack

    for rule in (given_name, whole_words, word_starts, anywhere):
        matches = [p for p in people if rule(p)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            names = ", ".join(_full_name(p) for p in matches)
            raise ToolError(
                f"More than one family member matches {name!r}: {names}. "
                "Ask which one they mean, then call again with the full name."
            )
    return None


def resolve_person_calendars(
    ctx: ToolContext, person: Optional[str]
) -> tuple[Optional[models.Person], List[Tuple[str, str]]]:
    """Turn a free-form ``person`` arg into ``(Person row, [(calendar_id, label), …])``.

    ``person`` may be a name, an email address, or empty / "me" for
    whoever is talking.

    Returns BOTH the personal and work calendar ids (when populated)
    so the availability / event tools read every calendar the person
    keeps. The label is one of:

    * ``"personal"`` — ``people.email_address``
    * ``"work"``     — the ``work_email`` on each of the person's jobs
    * ``"direct"``   — the caller passed an email address that
      didn't match any family member (we still try it as a
      single calendar so the agent isn't useless against a
      babysitter / handyman shared calendar).

    The returned list preserves order: personal first, work second.
    Callers can iterate it for the "personal calendar shows event
    titles, work calendar usually only shows busy" rendering rule.
    """
    needle = (person or "").strip()
    if not needle or needle.lower() in _SELF_WORDS:
        if ctx.person_id is None:
            return None, []
        speaker = ctx.db.get(models.Person, ctx.person_id)
        if speaker is None or speaker.family_id != ctx.family_id:
            return None, []
        return speaker, calendar_pairs_for(speaker)

    if looks_like_email(needle):
        # Match against EITHER the person's personal email_address OR
        # the work_email on ANY of their jobs, so "ben@work.io" still
        # resolves to the person row + pulls in their personal calendar
        # too. The jobs check is an EXISTS subquery so we don't have
        # to deduplicate the parent row when a person has multiple
        # jobs.
        lowered = needle.lower()
        from sqlalchemy import exists as _sa_exists

        job_email_match = _sa_exists().where(
            (models.Job.person_id == models.Person.person_id)
            & models.Job.work_email.ilike(needle)
        )
        match = (
            ctx.db.query(models.Person)
            .filter(models.Person.family_id == ctx.family_id)
            .filter(models.Person.email_address.ilike(needle) | job_email_match)
            .first()
        )
        if match is None:
            return None, [(needle, "direct")]
        return match, calendar_pairs_for(match, requested_email=lowered)

    chosen = find_family_member(ctx, needle)
    if chosen is None:
        return None, []
    return chosen, calendar_pairs_for(chosen)


def resolve_calendar_subject(
    ctx: ToolContext, person: Optional[str]
) -> tuple[Optional[models.Person], List[Tuple[str, str]]]:
    """Like :func:`resolve_person_calendars`, but never comes back empty.

    Raises :class:`ToolError` saying exactly what is missing — who is
    being asked about, or which email to add — so the reply to the user
    names the real problem instead of a generic "no calendar".
    """
    person_row, pairs = resolve_person_calendars(ctx, person)
    if pairs:
        return person_row, pairs

    needle = (person or "").strip()
    if person_row is not None:
        name = person_row.preferred_name or person_row.first_name
        raise ToolError(
            f"{name} has no email address on file, so I don't know which "
            "calendar is theirs. Add a personal email on their profile, "
            "or a work email under Jobs there, and I can check it."
        )
    if not needle or needle.lower() in _SELF_WORDS:
        raise ToolError(
            "I don't know who I'm talking to, so I can't tell whose "
            "calendar to check. Ask whose calendar they mean and pass "
            "that person's name."
        )
    known = ", ".join(
        sorted(
            p.preferred_name or p.first_name
            for p in ctx.db.query(models.Person)
            .filter(models.Person.family_id == ctx.family_id)
            .all()
        )
    )
    raise ToolError(
        f"Nobody in this household is called {needle!r}. "
        f"Family members: {known or 'none on file'}."
    )


def calendar_pairs_for(
    person: models.Person, *, requested_email: Optional[str] = None
) -> List[Tuple[str, str]]:
    """Return ``[(calendar_id, label), …]`` for the configured emails.

    When ``requested_email`` is provided AND it matches one of the
    person's emails, that one is placed first so a "Is X's work
    calendar free?" style ask still feels targeted; the other is
    appended (so we still merge in the rest for completeness).
    """
    pairs: List[Tuple[str, str]] = []
    personal = (person.email_address or "").strip()
    requested_l = (requested_email or "").lower()

    if personal:
        pairs.append((personal, "personal"))

    # A person can have multiple jobs; each job's work_email is its
    # own Google Calendar id (e.g. day job + side consulting). De-dupe
    # case-insensitively so a job rolled forward with the same email
    # doesn't double-count.
    seen_work: set[str] = set()
    for job in person.jobs or []:
        work = (job.work_email or "").strip()
        if not work:
            continue
        key = work.lower()
        if key in seen_work or key == personal.lower():
            continue
        seen_work.add(key)
        pairs.append((work, "work"))

    if requested_l:
        pairs.sort(key=lambda p: 0 if p[0].lower() == requested_l else 1)
    return pairs


def parse_iso_arg(label: str, value: str) -> datetime:
    """Parse an LLM-supplied ISO 8601 timestamp with a friendly error."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError(f"{label} is required (ISO 8601 datetime).")
    try:
        # Accept trailing 'Z' as UTC (Python pre-3.11 datetime is fussy).
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ToolError(
            f"{label} must be ISO 8601 (e.g. 2026-04-20T09:00:00-04:00). "
            f"Got {value!r}."
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_iso(value: str) -> str:
    """Turn an RFC3339 timestamp into a short human-friendly string."""
    try:
        dt = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return value
    return dt.strftime("%a %b %-d %-I:%M %p %Z").rstrip()


def assistant_email(ctx: ToolContext) -> Optional[str]:
    """Best-effort lookup of the assistant's connected Google address."""
    if ctx.assistant_id is None:
        return None
    row = google_oauth.load_credentials_row(ctx.db, ctx.assistant_id)
    return getattr(row, "granted_email", None) if row else None
