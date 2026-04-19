"""Tool registry the AI agent loop can dispatch into.

A *tool* here is a small, well-typed Python function the LLM can call
to read data or take an action. We deliberately keep the registry tiny
and explicit (no class auto-discovery) so it's obvious in code review
exactly what Avi is allowed to do.

Each tool declares:

* ``name`` — what the model uses in ``tool_calls``.
* ``description`` — short natural-language hint used by the model.
* ``parameters`` — JSON Schema for the arguments. We also use it to
  validate inputs before running the handler so a hallucinated
  argument shape can't crash the executor.
* ``handler`` — the actual Python callable. Receives a
  :class:`ToolContext` as its first argument and the model-supplied
  arguments as keyword args.
* ``timeout_seconds`` — hard deadline. Anything that runs longer is
  cancelled and the tool result becomes an error.
* ``requires`` — capability flags the tool needs (e.g. "google"). The
  agent can refuse to advertise a tool when its capability is missing
  rather than letting the model call it and get a confusing error.

Adding a new tool: define a handler, wrap it in :class:`Tool`, and
register it in :func:`build_default_registry`. That's it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..crypto import decrypt_str
from ..integrations import google_oauth
from ..integrations.gmail import GmailSendError, send_email
from ..integrations.google_calendar import (
    CalendarError,
    CalendarNotShared,
    PerCalendarBusy,
    busy_for_calendars,
    events_for_calendar,
    find_free_slots,
    list_upcoming_events,
    merge_busy_intervals,
)
from . import authz, sql_tool


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    db: Session
    family_id: int
    assistant_id: Optional[int] = None
    person_id: Optional[int] = None  # who is talking, when known


@dataclass
class ToolResult:
    """Structured outcome we feed back to the model after each call."""

    ok: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    # Optional human-friendly summary for the UI step card. The full
    # ``output`` JSON also goes to the audit row; this is just the
    # one-line "Sent message gAAA…=" displayed inline.
    summary: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Trim ``output`` for the model — keep it short to save tokens."""
        if self.ok:
            return {"ok": True, "output": _truncate_for_model(self.output)}
        return {"ok": False, "error": self.error}


def _truncate_for_model(value: Any, *, limit: int = 4000) -> Any:
    """Stringify + cap large outputs so we don't blow the context window."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > limit:
            return value[: limit - 12] + "…(truncated)"
        return value
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 12] + "…(truncated)"
    return text


ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)
    handler: ToolHandler
    timeout_seconds: float = 15.0
    requires: tuple[str, ...] = field(default_factory=tuple)
    # Human-friendly title for capability descriptions ("Send email"
    # rather than "gmail_send"). Falls back to ``name`` when omitted.
    label: Optional[str] = None
    # Sample user phrasings the LLM can quote back when asked "what
    # can you do?". Two-to-three short examples per tool keeps the
    # system prompt small while still being concrete.
    examples: tuple[str, ...] = field(default_factory=tuple)

    def display_label(self) -> str:
        return self.label or self.name

    def to_ollama_schema(self) -> Dict[str, Any]:
        """Render the tool in the Ollama / OpenAI ``tools`` shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Capability-aware collection of :class:`Tool` instances."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def for_capabilities(self, available: set[str]) -> List[Tool]:
        """Return only tools whose ``requires`` are satisfied."""
        out: List[Tool] = []
        for t in self._tools.values():
            if all(req in available for req in t.requires):
                out.append(t)
        return out

    def to_ollama_tools(self, available: set[str]) -> List[Dict[str, Any]]:
        return [t.to_ollama_schema() for t in self.for_capabilities(available)]

    async def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Run a tool by name with timeout + structured error capture."""
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"Unknown tool {name!r}. Available: {sorted(self._tools)}",
            )
        # Light validation — mostly a "did the model send a dict?" check.
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} expects an arguments object, got {type(arguments).__name__}",
            )
        # Validate required fields.
        required = tool.parameters.get("required", [])
        missing = [k for k in required if k not in arguments]
        if missing:
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} missing required argument(s): {missing}",
            )

        started = time.monotonic()
        try:
            result_value = await asyncio.wait_for(
                tool.handler(ctx, **arguments),
                timeout=tool.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                error=(
                    f"Tool {name!r} timed out after {tool.timeout_seconds:.0f}s"
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ToolError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - bubble as structured error
            logger.exception("Tool %r crashed", name)
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result_value, ToolResult):
            result_value.duration_ms = result_value.duration_ms or duration_ms
            return result_value
        return ToolResult(ok=True, output=result_value, duration_ms=duration_ms)


class ToolError(RuntimeError):
    """Raised inside a tool handler to surface a clean, user-facing error
    to the agent loop without dumping a traceback."""


# ---------------------------------------------------------------------------
# Concrete tools
# ---------------------------------------------------------------------------


# ---- sql.query --------------------------------------------------------


_SQL_QUERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "A single read-only SELECT (or WITH ... SELECT). Always "
                "scope by family_id where the table has it. Avoid SELECT *."
            ),
        }
    },
    "required": ["sql"],
}


async def _handle_sql_query(ctx: ToolContext, sql: str) -> Dict[str, Any]:
    # Compute the speaker's accessible-subject window once per call so
    # the SQL sanitiser can redact partially-sensitive columns
    # (currently ``people.notes``) on the way back. Fully sensitive
    # tables are blocked at the parser; this handles column-level
    # cases like notes.
    scope = authz.build_speaker_scope(ctx.db, speaker_person_id=ctx.person_id)
    try:
        result = sql_tool.run_safe_query(
            ctx.db,
            sql,
            family_id=ctx.family_id,
            max_rows=50,
            accessible_subject_ids=scope.can_access_subject_ids,
        )
    except sql_tool.SqlToolError as e:
        raise ToolError(str(e)) from e
    return {
        "row_count": result.row_count,
        "truncated": result.truncated,
        "columns": result.columns,
        "rows": result.rows,
    }


# ---- lookup_person ----------------------------------------------------


_LOOKUP_PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Name (or partial name / nickname) of the family member to "
                "look up. Matches first_name, preferred_name, and last_name "
                "case-insensitively."
            ),
        }
    },
    "required": ["name"],
}


async def _handle_lookup_person(ctx: ToolContext, name: str) -> List[Dict[str, Any]]:
    """Quick fuzzy lookup of a household member by name.

    Faster, more reliable, and more privacy-respecting than asking the
    model to write SQL for the trivial 'who is Sarah' case. Returns up
    to 5 matches with the columns the model needs to take action.
    """
    needle = (name or "").strip()
    if not needle:
        return []
    pattern = f"%{needle.lower()}%"
    rows = (
        ctx.db.query(models.Person)
        .filter(models.Person.family_id == ctx.family_id)
        .all()
    )
    matches: List[Dict[str, Any]] = []
    for p in rows:
        haystack = " ".join(
            x for x in (p.first_name, p.preferred_name, p.last_name) if x
        ).lower()
        if pattern.strip("%") in haystack:
            matches.append(
                {
                    "person_id": p.person_id,
                    "first_name": p.first_name,
                    "preferred_name": p.preferred_name,
                    "last_name": p.last_name,
                    "email_address": p.email_address,
                    "work_email": p.work_email,
                    "gender": p.gender,
                }
            )
            if len(matches) >= 5:
                break
    return matches


# ---- reveal_sensitive_identifier --------------------------------------


_REVEAL_SENSITIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the family member whose identifier should "
                "be revealed. Use lookup_person first if you only know "
                "their name."
            ),
        },
        "identifier_type": {
            "type": "string",
            "description": (
                "Which identifier to reveal — typically "
                "'social_security_number'. Other values are stored as-is "
                "in sensitive_identifiers.identifier_type."
            ),
        },
    },
    "required": ["person_id", "identifier_type"],
}


async def _handle_reveal_sensitive(
    ctx: ToolContext, person_id: int, identifier_type: str
) -> Dict[str, Any]:
    """Decrypt and return a person's SSN / tax ID — gated by relationship.

    This is the ONLY path that yields a full plaintext SSN. The check
    matches the user-stated rule: a person can always read their own,
    a parent can read their direct child's, spouses can read each
    other's, and everyone else (children, grandparents, siblings,
    in-laws, anonymous speakers) is denied. Every call — allow or
    deny — is logged via :mod:`ai.authz` so a future audit can answer
    "did Avi ever read Sarah's SSN, and who asked?".
    """
    if ctx.person_id is None:
        # An anonymous speaker can never decrypt anything. We refuse
        # without even leaking which subject was queried.
        raise ToolError(
            "I can't reveal sensitive identifiers without first "
            "knowing who is asking. Please greet me on camera (or "
            "email me from your registered address) and try again."
        )

    decision = authz.can_access_sensitive(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=int(person_id),
        family_id=ctx.family_id,
    )
    if not decision.allowed:
        raise ToolError(
            "I can't share that — household privacy rules only let a "
            "person see their own sensitive identifiers, plus those of "
            "their spouse and direct children. Please ask the person "
            "themselves (or one of their parents) for it."
        )

    rows = (
        ctx.db.query(models.SensitiveIdentifier)
        .filter(
            models.SensitiveIdentifier.person_id == int(person_id),
            models.SensitiveIdentifier.identifier_type == identifier_type,
        )
        .all()
    )
    if not rows:
        return {
            "found": False,
            "person_id": int(person_id),
            "identifier_type": identifier_type,
        }

    # If somehow there are multiple, return them all (e.g. someone has
    # both an SSN and a historical ITIN under the same type — unlikely
    # but cheap to support).
    revealed: List[Dict[str, Any]] = []
    for row in rows:
        try:
            plaintext = decrypt_str(row.identifier_value_encrypted)
        except RuntimeError as e:
            # Likely a key-mismatch (rotated FA_ENCRYPTION_KEY without
            # re-encrypting). Don't bubble the secret-y exception text
            # to the model — keep the error generic.
            logger.error(
                "Failed to decrypt sensitive_identifier_id=%s: %s",
                row.sensitive_identifier_id,
                e,
            )
            raise ToolError(
                "Stored value couldn't be decrypted with the current "
                "encryption key — flag this to the household admin."
            ) from e
        revealed.append(
            {
                "sensitive_identifier_id": row.sensitive_identifier_id,
                "identifier_type": row.identifier_type,
                "value": plaintext,
                "last_four": row.identifier_last_four,
            }
        )

    logger.info(
        "[authz] DECRYPT requestor=%s subject=%s identifier_type=%s count=%d",
        ctx.person_id,
        int(person_id),
        identifier_type,
        len(revealed),
    )
    return {
        "found": True,
        "person_id": int(person_id),
        "identifier_type": identifier_type,
        "results": revealed,
        "access_label": decision.label,
    }


# ---- gmail.send -------------------------------------------------------


_GMAIL_SEND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient email address (one).",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": (
                "Plain-text email body. Sign off naturally as the assistant; "
                "do not include the recipient's name in the signature."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}


async def _handle_gmail_send(
    ctx: ToolContext, to: str, subject: str, body: str
) -> ToolResult:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — connect one in the admin UI."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    try:
        message_id = await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as e:
        raise ToolError(str(e)) from e

    return ToolResult(
        ok=True,
        output={"message_id": message_id, "to": to, "subject": subject},
        summary=f"Sent “{subject}” to {to}",
    )


# ---- calendar.list_upcoming -------------------------------------------


_CALENDAR_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "hours_ahead": {
            "type": "integer",
            "description": "How many hours into the future to scan. Default 72.",
            "minimum": 1,
            "maximum": 720,
        },
        "max_results": {
            "type": "integer",
            "description": "Max events to return across all calendars. Default 15.",
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": [],
}


async def _handle_calendar_list(
    ctx: ToolContext,
    hours_ahead: int = 72,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e
    try:
        events = await asyncio.to_thread(
            list_upcoming_events,
            creds,
            hours_ahead=hours_ahead,
            max_results=max_results,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e
    return [
        {
            "summary": e.summary,
            "start": e.start,
            "end": e.end,
            "location": e.location,
            "calendar_id": e.calendar_id,
        }
        for e in events
    ]


# ---- shared helpers for the per-person freebusy tools -----------------


def _looks_like_email(value: str) -> bool:
    """Lightweight email heuristic — good enough to branch the resolver."""
    return "@" in value and "." in value.split("@", 1)[1]


def _resolve_person_calendars(
    ctx: ToolContext, person: str
) -> tuple[Optional[models.Person], List[tuple[str, str]]]:
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

    if _looks_like_email(needle):
        # Match against EITHER personal or work email so "ben@work.io"
        # still resolves to the person row + pulls in their personal
        # calendar too.
        lowered = needle.lower()
        match = (
            ctx.db.query(models.Person)
            .filter(models.Person.family_id == ctx.family_id)
            .filter(
                models.Person.email_address.ilike(needle)
                | models.Person.work_email.ilike(needle)
            )
            .first()
        )
        if match is None:
            return None, [(needle, "direct")]
        return match, _calendar_pairs_for(match, requested_email=lowered)

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
    return chosen, _calendar_pairs_for(chosen)


def _calendar_pairs_for(
    person: models.Person, *, requested_email: Optional[str] = None
) -> List[tuple[str, str]]:
    """Return ``[(calendar_id, label), …]`` for the configured emails.

    When ``requested_email`` is provided AND it matches one of the
    person's emails, that one is placed first so a "Is X's work
    calendar free?" style ask still feels targeted; the other is
    appended (so we still merge in the rest for completeness).
    """
    pairs: List[tuple[str, str]] = []
    personal = (person.email_address or "").strip()
    work = (person.work_email or "").strip()
    requested_l = (requested_email or "").lower()

    if personal:
        pairs.append((personal, "personal"))
    if work:
        pairs.append((work, "work"))

    if requested_l:
        pairs.sort(key=lambda p: 0 if p[0].lower() == requested_l else 1)
    return pairs


# Back-compat shim: a couple of (older) callers import the previous
# single-email helper. Returns the personal email so existing code
# keeps working unchanged; new calendar code paths should use
# :func:`_resolve_person_calendars`.
def _resolve_person_email(
    ctx: ToolContext, person: str
) -> tuple[Optional[models.Person], Optional[str]]:
    p, pairs = _resolve_person_calendars(ctx, person)
    return p, (pairs[0][0] if pairs else None)


def _parse_iso_arg(label: str, value: str) -> datetime:
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


def _humanize_iso(value: str) -> str:
    """Turn an RFC3339 timestamp into a short human-friendly string."""
    try:
        dt = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return value
    return dt.strftime("%a %b %-d %-I:%M %p %Z").rstrip()


# ---- calendar.check_availability --------------------------------------


_CALENDAR_CHECK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name (e.g. 'Ben') or email address of the household "
                "member whose schedule you want to check. Names are "
                "matched fuzzy against first / preferred / last."
            ),
        },
        "start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the window (e.g. "
                "2026-04-20T13:00:00-04:00). Include the timezone "
                "offset that matches the user's intent."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the window. Must be after start."
            ),
        },
    },
    "required": ["person", "start", "end"],
}


async def _handle_calendar_check_availability(
    ctx: ToolContext, person: str, start: str, end: str
) -> Dict[str, Any]:
    """Answer 'is X free between A and B?' across BOTH of X's calendars.

    Resolves ``person`` to their personal AND work emails, runs a
    single freebusy query against both, and returns:

    * ``per_calendar`` — one entry per calendar with shared/busy and
      the label (``personal`` / ``work`` / ``direct``). Lets the
      model say "His personal calendar isn't shared, but his work
      calendar shows him busy 2-3."
    * ``busy`` — the merged busy intervals across all SHARED
      calendars (sorted, overlap-merged). The "is X free?" answer
      should use this list — they're free if it's empty.
    * ``summary`` — a short natural-language phrasing the model can
      crib for its reply. Mentions any calendar that wasn't shared
      so the user knows to ask for that share.

    Free/busy intervals carry NO event detail — no titles, no
    locations — so we don't apply the calendar-detail relationship
    gate here. Anyone in the household can see whether anyone else
    is free or busy. Detail-level access is gated separately by
    :func:`calendar_list_for_person`.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile in the admin "
            "console (or pass an email address directly) and I can "
            "check their calendar."
        )

    start_dt = _parse_iso_arg("start", start)
    end_dt = _parse_iso_arg("end", end)
    if end_dt <= start_dt:
        raise ToolError("end must be strictly after start.")

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    display_name = (
        person_row.preferred_name or person_row.first_name
        if person_row
        else calendar_pairs[0][0]
    )

    try:
        per_cal = await asyncio.to_thread(
            busy_for_calendars,
            creds,
            calendars=calendar_pairs,
            start=start_dt,
            end=end_dt,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e

    merged_busy = merge_busy_intervals(per_cal)
    summary = _summarise_availability(display_name, per_cal, merged_busy)

    return {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": any(b.shared for b in per_cal),
        "busy": merged_busy,
        "summary": summary,
    }


def _per_cal_payload(b: PerCalendarBusy) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "calendar_id": b.calendar_id,
        "label": b.label,
        "shared": b.shared,
    }
    if b.shared:
        out["busy"] = b.busy
    else:
        out["reason"] = b.reason
    return out


def _summarise_availability(
    display_name: str,
    per_cal: List[PerCalendarBusy],
    merged_busy: List[dict],
) -> str:
    shared = [b for b in per_cal if b.shared]
    not_shared = [b for b in per_cal if not b.shared]

    if not shared:
        labels = ", ".join(f"{b.label} ({b.calendar_id})" for b in per_cal)
        return (
            f"None of {display_name}'s calendars are shared with me "
            f"({labels}). Ask them to share at least one with this "
            "assistant under Google Calendar → Settings → Share with "
            "specific people."
        )

    if not merged_busy:
        head = f"{display_name} is free across the entire window."
    else:
        first = merged_busy[0]
        more = len(merged_busy) - 1
        head = (
            f"{display_name} is busy "
            f"{_humanize_iso(first['start'])} – {_humanize_iso(first['end'])}"
            + (f" (and {more} more conflict(s))." if more > 0 else ".")
        )

    if not_shared:
        unshared_labels = ", ".join(b.label for b in not_shared)
        head += (
            f" Note: their {unshared_labels} calendar isn't shared "
            "with me, so this only reflects the calendars I can see."
        )
    return head


# ---- calendar.find_free_slots -----------------------------------------


_CALENDAR_FREE_SLOTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member to find time for."
            ),
        },
        "window_start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the search window (typically the "
                "earliest the user is willing to consider, e.g. tomorrow "
                "morning at 9am local time)."
            ),
        },
        "window_end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the search window (e.g. end of next "
                "week)."
            ),
        },
        "duration_minutes": {
            "type": "integer",
            "description": "Length of the desired free slot. Default 30.",
            "minimum": 5,
            "maximum": 480,
        },
        "working_hours_only": {
            "type": "boolean",
            "description": (
                "When true (default), only suggest slots between 9am "
                "and 6pm local time on each day. Set false for "
                "evenings / weekends explicitly."
            ),
        },
        "max_slots": {
            "type": "integer",
            "description": "Max number of suggestions. Default 5.",
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["person", "window_start", "window_end"],
}


async def _handle_calendar_find_free_slots(
    ctx: ToolContext,
    person: str,
    window_start: str,
    window_end: str,
    duration_minutes: int = 30,
    working_hours_only: bool = True,
    max_slots: int = 5,
) -> Dict[str, Any]:
    """Suggest open windows in a person's calendar.

    Queries BOTH the personal and work calendars (when configured),
    merges their busy intervals, and feeds the union into the pure
    :func:`find_free_slots` helper. A slot is only "free" if BOTH
    calendars are free at that time — exactly what you want when
    booking around someone's day job.

    If a calendar exists on the person's profile but isn't shared
    with the assistant, we still return slots from the calendars we
    CAN see and warn in ``summary`` so the user knows the
    suggestions might miss a hidden conflict.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile in the admin "
            "console (or pass an email address directly) and I can "
            "suggest a time."
        )

    start_dt = _parse_iso_arg("window_start", window_start)
    end_dt = _parse_iso_arg("window_end", window_end)
    if end_dt <= start_dt:
        raise ToolError("window_end must be strictly after window_start.")
    if (end_dt - start_dt).total_seconds() > 31 * 24 * 3600:
        raise ToolError(
            "Window is too wide — please limit to about a month of "
            "search time per call so the suggestions stay useful."
        )

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    display_name = (
        person_row.preferred_name or person_row.first_name
        if person_row
        else calendar_pairs[0][0]
    )

    try:
        per_cal = await asyncio.to_thread(
            busy_for_calendars,
            creds,
            calendars=calendar_pairs,
            start=start_dt,
            end=end_dt,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e

    if not any(b.shared for b in per_cal):
        labels = ", ".join(f"{b.label} ({b.calendar_id})" for b in per_cal)
        return {
            "person": display_name,
            "per_calendar": [_per_cal_payload(b) for b in per_cal],
            "any_shared": False,
            "slots": [],
            "summary": (
                f"None of {display_name}'s calendars ({labels}) are "
                f"shared with me, so I can't suggest free times. Ask "
                f"them to share at least one with "
                f"{_assistant_email(ctx) or 'this assistant'} under "
                "Google Calendar → Settings → Share with specific "
                "people."
            ),
        }

    merged_busy = merge_busy_intervals(per_cal)

    # Honour the original ISO offset so working-hours-only suggestions
    # land in the user's local day, not UTC.
    local_tz = start_dt.tzinfo or timezone.utc
    slots = find_free_slots(
        busy=merged_busy,
        window_start=start_dt,
        window_end=end_dt,
        duration_minutes=duration_minutes,
        working_hours=(9, 18) if working_hours_only else None,
        max_slots=max_slots,
        tz=local_tz,
    )

    not_shared = [b for b in per_cal if not b.shared]
    if not slots:
        summary = (
            f"I couldn't find a {duration_minutes}-minute slot for "
            f"{display_name} in that window — they look booked through "
            "it. Try widening the window or relaxing working_hours_only."
        )
    else:
        first = slots[0]
        summary = (
            f"Suggested time for {display_name}: "
            f"{_humanize_iso(first['start'])} – "
            f"{_humanize_iso(first['end'])}"
            + (
                f" (plus {len(slots) - 1} more option(s))."
                if len(slots) > 1
                else "."
            )
        )
    if not_shared:
        unshared_labels = ", ".join(b.label for b in not_shared)
        summary += (
            f" Heads up: their {unshared_labels} calendar isn't "
            "shared with me, so a hidden conflict on it could still "
            "land in one of these suggested slots."
        )

    return {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": True,
        "duration_minutes": duration_minutes,
        "working_hours_only": working_hours_only,
        "slots": slots,
        "summary": summary,
    }


# ---- calendar.list_for_person -----------------------------------------


_CALENDAR_LIST_FOR_PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member whose events "
                "you want to list."
            ),
        },
        "window_start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the window (e.g. start of "
                "this week)."
            ),
        },
        "window_end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the window (e.g. end of "
                "this week)."
            ),
        },
        "max_results_per_calendar": {
            "type": "integer",
            "description": (
                "Cap on events returned per calendar. Default 25."
            ),
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["person", "window_start", "window_end"],
}


async def _handle_calendar_list_for_person(
    ctx: ToolContext,
    person: str,
    window_start: str,
    window_end: str,
    max_results_per_calendar: int = 25,
) -> Dict[str, Any]:
    """List events on a person's personal + work calendars.

    Applies the calendar-detail relationship gate
    (:func:`authz.can_see_calendar_details`):

    * The speaker IS the subject, OR is the subject's spouse —
      events come back with full detail (summary, location,
      organizer, calendar label).
    * Anyone else (parents, children, siblings, in-laws, anonymous
      speakers) — events come back with summary/location replaced
      by ``[busy — private]`` and only their start / end / calendar
      label exposed. The reader still sees WHEN the person is
      busy but NOT what they're doing.

    A calendar that exists on the profile but isn't shared with
    the assistant comes back as ``shared=False`` with a hint to
    ask for the share.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile and I can list "
            "their calendar."
        )
    if person_row is None:
        # Direct-email lookup against a non-family calendar — refuse
        # to list events: we can't run the relationship gate without
        # a Person, and silently leaking detail would be wrong.
        raise ToolError(
            f"I can only list calendar events for registered family "
            f"members. {person!r} isn't one of them — try giving me "
            "their name as it appears in the admin console."
        )

    start_dt = _parse_iso_arg("window_start", window_start)
    end_dt = _parse_iso_arg("window_end", window_end)
    if end_dt <= start_dt:
        raise ToolError("window_end must be strictly after window_start.")
    if (end_dt - start_dt).total_seconds() > 31 * 24 * 3600:
        raise ToolError(
            "Window is too wide — please limit to about a month per call."
        )

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    detail_decision = authz.can_see_calendar_details(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=person_row.person_id,
        family_id=ctx.family_id,
    )
    show_detail = detail_decision.allowed
    display_name = person_row.preferred_name or person_row.first_name

    per_calendar_out: List[Dict[str, Any]] = []
    total_events = 0
    any_shared = False

    for cal_id, label in calendar_pairs:
        try:
            events = await asyncio.to_thread(
                events_for_calendar,
                creds,
                calendar_id=cal_id,
                start=start_dt,
                end=end_dt,
                max_results=max_results_per_calendar,
            )
        except CalendarNotShared as e:
            per_calendar_out.append(
                {
                    "calendar_id": cal_id,
                    "label": label,
                    "shared": False,
                    "reason": e.reason,
                }
            )
            continue
        except CalendarError as e:
            raise ToolError(str(e)) from e

        any_shared = True
        rendered: List[Dict[str, Any]] = []
        for ev in events:
            if show_detail:
                rendered.append(
                    {
                        "start": ev.start,
                        "end": ev.end,
                        "summary": ev.summary,
                        "location": ev.location,
                        "organizer_email": ev.organizer_email,
                    }
                )
            else:
                rendered.append(
                    {
                        "start": ev.start,
                        "end": ev.end,
                        "summary": "[busy — private]",
                        "location": None,
                        "organizer_email": None,
                    }
                )
        total_events += len(rendered)
        per_calendar_out.append(
            {
                "calendar_id": cal_id,
                "label": label,
                "shared": True,
                "events": rendered,
            }
        )

    if not any_shared:
        summary = (
            f"None of {display_name}'s calendars are shared with me. "
            f"Ask them to share at least one with "
            f"{_assistant_email(ctx) or 'this assistant'}."
        )
    elif total_events == 0:
        summary = (
            f"{display_name} has no events on their shared "
            "calendars in that window."
        )
    elif show_detail:
        summary = (
            f"{display_name} has {total_events} event(s) in that "
            "window — you (and their spouse) are allowed to see the "
            "full detail."
        )
    else:
        summary = (
            f"{display_name} has {total_events} busy slot(s) in that "
            "window. Per household privacy rules I'm only sharing "
            "free/busy with you, not the event titles. Ask "
            f"{display_name} themselves (or their spouse) for "
            "specifics."
        )

    return {
        "person": display_name,
        "show_detail": show_detail,
        "access_label": detail_decision.label,
        "per_calendar": per_calendar_out,
        "any_shared": any_shared,
        "total_events": total_events,
        "summary": summary,
    }


def _assistant_email(ctx: ToolContext) -> Optional[str]:
    """Best-effort lookup of the assistant's connected Google address."""
    if ctx.assistant_id is None:
        return None
    row = google_oauth.load_credentials_row(ctx.db, ctx.assistant_id)
    return getattr(row, "granted_email", None) if row else None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_default_registry() -> ToolRegistry:
    """Construct the registry the chat agent uses by default."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="sql_query",
            label="Query the family database",
            description=(
                "Run a single read-only SELECT against the family database. "
                "Useful for ad-hoc lookups (vehicles, residences, insurance, "
                "etc.) when the prebuilt tools below don't fit. Always include "
                "family_id in the WHERE clause."
            ),
            parameters=_SQL_QUERY_SCHEMA,
            handler=_handle_sql_query,
            timeout_seconds=8.0,
            examples=(
                "How many cars do we own?",
                "When does our auto insurance renew?",
                "Who in the family takes blood pressure medication?",
            ),
        )
    )
    reg.register(
        Tool(
            name="lookup_person",
            label="Look up a family member",
            description=(
                "Find a household member by partial name. Returns person_id, "
                "names, email, and gender. Use this BEFORE drafting an email "
                "to a family member so you have their real address."
            ),
            parameters=_LOOKUP_PERSON_SCHEMA,
            handler=_handle_lookup_person,
            timeout_seconds=4.0,
            examples=(
                "What's Sarah's email address?",
                "Tell me about Ben.",
            ),
        )
    )
    reg.register(
        Tool(
            name="reveal_sensitive_identifier",
            label="Reveal a sensitive identifier (SSN, tax ID)",
            description=(
                "Decrypt and return a family member's full sensitive "
                "identifier (typically Social Security Number). The tool "
                "enforces relationship-based privacy: it ONLY returns a "
                "value when the speaker is the subject themselves, the "
                "subject's spouse, or one of the subject's direct "
                "parents. Children, grandparents, siblings, in-laws, "
                "and anonymous speakers are refused. Every call is "
                "audit-logged. Use this only when the user explicitly "
                "asks for the full number; otherwise stick to the "
                "*_last_four helper columns."
            ),
            parameters=_REVEAL_SENSITIVE_SCHEMA,
            handler=_handle_reveal_sensitive,
            timeout_seconds=5.0,
            examples=(
                "What's my SSN?",
                "Read me my daughter's social security number.",
            ),
        )
    )
    reg.register(
        Tool(
            name="gmail_send",
            label="Send an email",
            description=(
                "Send a plain-text email from the assistant's connected "
                "Gmail account. Returns the Gmail message_id on success. "
                "ONLY call this once you have the recipient's real email "
                "address (use lookup_person first if needed) and a fully-"
                "drafted subject and body."
            ),
            parameters=_GMAIL_SEND_SCHEMA,
            handler=_handle_gmail_send,
            timeout_seconds=20.0,
            requires=("google",),
            examples=(
                "Send Mom a note thanking her for dinner.",
                "Email Ben a one-line summary of tomorrow's calendar.",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_upcoming",
            label="Read the calendar",
            description=(
                "List events on the assistant's connected Google calendar "
                "(and any calendars shared with it) for the next N hours. "
                "Use to answer 'what's coming up' or to gather context for "
                "an email about a future event."
            ),
            parameters=_CALENDAR_LIST_SCHEMA,
            handler=_handle_calendar_list,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on the calendar this week?",
                "Are we free Saturday afternoon?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_check_availability",
            label="Check a person's free/busy",
            description=(
                "Check whether one specific household member is free "
                "or busy in a given time window. Hits Google freebusy "
                "against BOTH the person's personal calendar "
                "(email_address) AND their work calendar (work_email) "
                "when both are configured, and merges the results so "
                "a slot only counts as free if the person is free on "
                "both. Returns per_calendar so you can mention if a "
                "specific calendar isn't shared with the assistant. "
                "Free/busy contains NO event detail (titles, "
                "locations) so this tool is safe to call for any "
                "household member regardless of who is asking."
            ),
            parameters=_CALENDAR_CHECK_SCHEMA,
            handler=_handle_calendar_check_availability,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Is Ben free Friday afternoon?",
                "Is Mom busy at 3pm tomorrow?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_find_free_slots",
            label="Find a free time for someone",
            description=(
                "Suggest open time slots for one household member "
                "across a window (up to ~1 month). Considers BOTH "
                "personal and work calendars when configured — a "
                "suggested slot is only free if the person is free "
                "on every shared calendar. Defaults to 30-minute "
                "slots inside 9am-6pm working hours, configurable. "
                "Warns if any calendar exists on the profile but "
                "isn't shared with the assistant."
            ),
            parameters=_CALENDAR_FREE_SLOTS_SCHEMA,
            handler=_handle_calendar_find_free_slots,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Find me a time Ben is free next week.",
                "When can Sarah do a 45-minute call this Thursday?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_for_person",
            label="List events for one person",
            description=(
                "List the actual events on a household member's "
                "personal AND work calendars between two timestamps. "
                "Honours the household's calendar-detail privacy "
                "rule: only the SUBJECT and their SPOUSE see event "
                "titles / locations; everyone else (parents, "
                "children, siblings, in-laws) gets the timing as "
                "free/busy with the title replaced by '[busy — "
                "private]'. Use when the user wants 'what's on "
                "Ben's schedule this week?' style detail. For 'is X "
                "free?' use calendar_check_availability instead — "
                "it's cheaper and doesn't leak detail."
            ),
            parameters=_CALENDAR_LIST_FOR_PERSON_SCHEMA,
            handler=_handle_calendar_list_for_person,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on Ben's calendar this week?",
                "Show me Sarah's meetings tomorrow.",
            ),
        )
    )
    return reg


def describe_capabilities(
    registry: ToolRegistry, available: set[str]
) -> str:
    """Render the registry as a friendly bullet list for the system prompt.

    The model uses this to answer "what can you do?" / "help" with
    accurate, up-to-date answers instead of making them up. Tools
    whose capabilities aren't satisfied (e.g. Google not connected)
    are silently omitted so the model never offers things it can't
    actually do this turn.
    """
    tools_available = registry.for_capabilities(available)
    if not tools_available:
        return ""
    lines: List[str] = ["You currently have these tools:"]
    for t in tools_available:
        lines.append(f"- {t.display_label()} ({t.name}) — {t.description}")
        for ex in t.examples[:2]:
            lines.append(f'    e.g. "{ex}"')
    lines.append("")
    lines.append(
        "When the user asks 'what can you do?', 'help', or similar, "
        "summarise these capabilities in 2-4 friendly sentences. Quote "
        "ONE concrete example per capability so they know how to ask. "
        "Do not promise capabilities that aren't in the list above."
    )
    return "\n".join(lines)


def detect_capabilities(db: Session, assistant_id: Optional[int]) -> set[str]:
    """Inspect the database to figure out which capabilities are live.

    Today the only feature-flag-style capability is 'google' (= the
    assistant has connected an OAuth-authorised Google account with at
    least one usable scope). Add more here as we wire integrations.
    """
    caps: set[str] = set()
    if assistant_id is not None:
        row = google_oauth.load_credentials_row(db, assistant_id)
        if row is not None:
            scopes = set((row.scopes or "").split())
            if (
                any(s.endswith("/gmail.send") for s in scopes)
                or any(s.endswith("/gmail.modify") for s in scopes)
                or any(s.endswith("/calendar.readonly") for s in scopes)
            ):
                caps.add("google")
    return caps


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "describe_capabilities",
    "detect_capabilities",
]
