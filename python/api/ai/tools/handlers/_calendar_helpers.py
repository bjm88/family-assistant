"""Shared helpers used by every ``calendar_*`` tool.

Lives in its own module so :mod:`calendar` and the per-person freebusy
tools can share the person-resolution + ISO parsing code without
either side needing to import the other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .... import models
from ....integrations import google_oauth
from .._registry import ToolContext, ToolError


def looks_like_email(value: str) -> bool:
    """Lightweight email heuristic — good enough to branch the resolver."""
    return "@" in value and "." in value.split("@", 1)[1]


def resolve_person_calendars(
    ctx: ToolContext, person: str
) -> tuple[Optional[models.Person], List[Tuple[str, str]]]:
    """Turn a free-form ``person`` arg into ``(Person row, [(calendar_id, label), …])``.

    Returns BOTH the personal and work calendar ids (when populated)
    so the freebusy / event tools can hit them in a single Google
    request. The label is one of:

    * ``"personal"`` — ``Person.email_address``
    * ``"work"``     — ``Person.work_email``
    * ``"direct"``   — the caller passed an email address that
      didn't match any family member (we still try it as a
      single calendar so the agent isn't useless against a
      babysitter / handyman shared calendar).

    The returned list preserves order: personal first, work second.
    Callers can iterate it for the "personal calendar shows event
    titles, work calendar usually only shows busy" rendering rule.
    """
    needle = (person or "").strip()
    if not needle:
        return None, []

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

    rows = (
        ctx.db.query(models.Person)
        .filter(models.Person.family_id == ctx.family_id)
        .all()
    )
    pattern = needle.lower()
    matches: List[models.Person] = []
    for p in rows:
        haystack = " ".join(
            x for x in (p.first_name, p.preferred_name, p.last_name) if x
        ).lower()
        if pattern in haystack:
            matches.append(p)
    if not matches:
        return None, []
    # Prefer an exact first-name / preferred-name match when there are
    # multiple hits ("Sam" → Sam, not Samantha) so a kid's question
    # doesn't accidentally pull a parent.
    exact = [
        p
        for p in matches
        if (p.first_name or "").lower() == pattern
        or (p.preferred_name or "").lower() == pattern
    ]
    chosen = exact[0] if exact else matches[0]
    return chosen, calendar_pairs_for(chosen)


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
