"""All ``calendar_*`` tools.

* ``calendar_list_upcoming``      — what's on the assistant's calendar
* ``calendar_check_availability`` — free/busy for one person, with a
                                    day-by-day load for longer windows
* ``calendar_find_free_slots``    — suggest open windows
* ``calendar_create_event``       — write an event to the speaker's own cal
* ``calendar_list_for_person``    — events for one household member, with
                                    privacy gating on title/location

Every per-person tool answers the same three questions in the same
order, so the reply can say what it is based on:

1. **Who is this about?** The speaker ("I" / "me" / no name given), a
   named family member, or an email address
   (:func:`resolve_calendar_subject`).
2. **Which calendars are theirs, and what can the assistant see on
   each?** Their personal email plus the work email on each of their
   jobs; each comes back as event details visible, busy times only,
   or not shared (:func:`_calendar_access`).
3. **What does that add up to?** One merged picture across the
   calendars the assistant can see, with ``coverage`` saying whether
   that is all of them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .... import models
from ....integrations import google_oauth
from ....integrations.google_calendar import (
    CalendarError,
    CalendarNotShared,
    CalendarReadOnly,
    PerCalendarBusy,
    busy_by_day,
    busy_for_calendars,
    create_event as gcal_create_event,
    find_free_slots,
    list_upcoming_events,
    merge_busy_intervals,
    read_calendar_window,
)
from ... import authz
from ...time_context import family_timezone
from .._registry import ToolContext, ToolError
from ._calendar_helpers import (
    assistant_email,
    calendar_pairs_for,
    humanize_iso,
    parse_iso_arg,
    resolve_calendar_subject,
    resolve_person_calendars,
)

_PERSON_ARG_HINT = (
    " Leave this out (or pass 'me') when the user is asking about "
    "themselves — it defaults to the person you are talking to."
)


# ---- calendar.list_upcoming -------------------------------------------


CALENDAR_LIST_SCHEMA: Dict[str, Any] = {
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


async def handle_calendar_list(
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


# ---- calendar.check_availability --------------------------------------


CALENDAR_CHECK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name (e.g. 'Ben') or email address of the household "
                "member whose schedule you want to check. Names are "
                "matched against first / preferred / last."
                + _PERSON_ARG_HINT
            ),
        },
        "start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the window (e.g. "
                "2026-04-20T13:00:00-04:00). Include the timezone "
                "offset that matches the user's intent. For a "
                "whole-day or whole-week question start at 00:00 on "
                "the first day."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the window. Must be after start. For "
                "a whole-day or whole-week question end at 23:59 on "
                "the last day."
            ),
        },
    },
    "required": ["start", "end"],
}


async def handle_calendar_check_availability(
    ctx: ToolContext, start: str, end: str, person: Optional[str] = None
) -> Dict[str, Any]:
    """Answer 'is X free between A and B?' across BOTH of X's calendars.

    Resolves ``person`` to their personal AND work emails, looks up
    busy time on both, and returns:

    * ``per_calendar`` — one entry per calendar with shared/busy and
      the label (``personal`` / ``work`` / ``direct``). Lets the
      model say "His personal calendar isn't shared, but his work
      calendar shows him busy 2-3."
    * ``busy`` — the merged busy intervals across all SHARED
      calendars (sorted, overlap-merged). The "is X free?" answer
      should use this list — they're free if it's empty.
    * ``days`` / ``days_least_to_most_busy`` — for a window longer
      than a day, the busy hours on each date, so "which day is X
      most free?" is read off the result rather than added up from
      raw intervals.
    * ``coverage`` — ``complete`` / ``partial`` / ``none``: whether
      the answer reflects all of the person's calendars.
    * ``summary`` — a short natural-language phrasing the model can
      crib for its reply. Says which calendars it is based on and
      mentions any that weren't shared so the user knows to ask.

    Free/busy intervals carry NO event detail — no titles, no
    locations — so we don't apply the calendar-detail relationship
    gate here. Anyone in the household can see whether anyone else
    is free or busy. Detail-level access is gated separately by
    :func:`calendar_list_for_person`.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = resolve_calendar_subject(ctx, person)

    start_dt = parse_iso_arg("start", start)
    end_dt = parse_iso_arg("end", end)
    if end_dt <= start_dt:
        raise ToolError("end must be strictly after start.")
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
    # No visible calendar means "unknown", not "free": a breakdown of
    # empty days would read as an open week.
    by_day = (
        _day_breakdown(ctx, merged_busy, start_dt, end_dt)
        if any(b.shared for b in per_cal)
        else {}
    )
    summary = _summarise_availability(
        display_name, per_cal, merged_busy, by_day.get("sentence")
    )

    out: Dict[str, Any] = {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": any(b.shared for b in per_cal),
        "coverage": _coverage(per_cal),
        "busy": merged_busy,
    }
    if by_day:
        out["days"] = by_day["days"]
        out["days_least_to_most_busy"] = by_day["ranking"]
    out["summary"] = summary
    return out


def _calendar_access(b: PerCalendarBusy) -> str:
    """What the assistant can see on one of a person's calendars."""
    if not b.shared:
        return "not_shared"
    return "event_details" if b.details_visible else "busy_times_only"


def _per_cal_payload(b: PerCalendarBusy) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "calendar_id": b.calendar_id,
        "label": b.label,
        "shared": b.shared,
        "access": _calendar_access(b),
    }
    if b.shared:
        out["busy"] = b.busy
    else:
        out["reason"] = b.reason
    return out


def _coverage(per_cal: List[PerCalendarBusy]) -> str:
    """Does the answer reflect all, some or none of the person's calendars?"""
    shared = sum(1 for b in per_cal if b.shared)
    if shared == 0:
        return "none"
    return "complete" if shared == len(per_cal) else "partial"


def _calendar_name(label: str, calendar_id: str, note: str = "") -> str:
    kind = f"{label} calendar" if label in ("personal", "work") else "calendar"
    return f"{kind} ({calendar_id}{', ' + note if note else ''})"


def _based_on(display_name: str, per_cal: List[PerCalendarBusy]) -> str:
    """One sentence naming the calendars an answer was drawn from."""
    seen = [
        _calendar_name(
            b.label, b.calendar_id, "" if b.details_visible else "busy times only"
        )
        for b in per_cal
        if b.shared
    ]
    text = f" Based on {display_name}'s " + " and ".join(seen) + "."
    hidden = [_calendar_name(b.label, b.calendar_id) for b in per_cal if not b.shared]
    if hidden:
        text += (
            f" Their {' and '.join(hidden)} isn't shared with me, so "
            "anything on it is missing from this answer."
        )
    return text


def _day_label(day: Dict[str, Any]) -> str:
    return datetime.fromisoformat(day["date"]).strftime("%A, %b %-d")


def _day_breakdown(
    ctx: ToolContext,
    merged_busy: List[dict],
    start_dt: datetime,
    end_dt: datetime,
) -> Dict[str, Any]:
    """Per-day busy hours for a window spanning more than one day.

    Returns ``{}`` for a window inside a single day. Otherwise
    ``days`` (in date order), ``ranking`` (least busy first, as short
    strings) and a ``sentence`` naming the lightest and heaviest days
    with their dates. Days are the household's local days. A first or
    last day the window only partly covers is listed but left out of
    the ranking.
    """
    days = busy_by_day(
        merged_busy,
        window_start=start_dt,
        window_end=end_dt,
        tz=family_timezone(ctx.db, ctx.family_id),
    )
    if len(days) < 2:
        return {}
    whole = [d for d in days if not d["partial_day"]] or days
    ranked = sorted(whole, key=lambda d: (d["busy_hours"], d["date"]))
    least, most = ranked[0]["busy_hours"], ranked[-1]["busy_hours"]
    lightest = [d for d in ranked if d["busy_hours"] == least]
    if least == 0:
        sentence = "Nothing booked on " + ", ".join(_day_label(d) for d in lightest) + "."
    else:
        sentence = (
            "Least booked: "
            + ", ".join(_day_label(d) for d in lightest)
            + f" ({least:g}h busy)."
        )
    if most > least:
        sentence += f" Most booked: {_day_label(ranked[-1])} ({most:g}h busy)."
    return {
        "days": days,
        "ranking": [
            f"{_day_label(d)}: {d['busy_hours']:g}h busy" for d in ranked
        ],
        "sentence": sentence,
    }


def _summarise_availability(
    display_name: str,
    per_cal: List[PerCalendarBusy],
    merged_busy: List[dict],
    day_sentence: Optional[str] = None,
) -> str:
    if not any(b.shared for b in per_cal):
        labels = ", ".join(f"{b.label} ({b.calendar_id})" for b in per_cal)
        return (
            f"None of {display_name}'s calendars are shared with me "
            f"({labels}). Ask them to share at least one with this "
            "assistant under Google Calendar → Settings → Share with "
            "specific people."
        )

    if not merged_busy:
        head = f"{display_name} is free across the entire window."
    elif day_sentence:
        head = f"{display_name}'s schedule by day: {day_sentence}"
    else:
        first = merged_busy[0]
        more = len(merged_busy) - 1
        head = (
            f"{display_name} is busy "
            f"{humanize_iso(first['start'])} – {humanize_iso(first['end'])}"
            + (f" (and {more} more conflict(s))." if more > 0 else ".")
        )
    return head + _based_on(display_name, per_cal)


# ---- calendar.find_free_slots -----------------------------------------


CALENDAR_FREE_SLOTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member to find time for."
                + _PERSON_ARG_HINT
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
    "required": ["window_start", "window_end"],
}


async def handle_calendar_find_free_slots(
    ctx: ToolContext,
    window_start: str,
    window_end: str,
    person: Optional[str] = None,
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
    person_row, calendar_pairs = resolve_calendar_subject(ctx, person)

    start_dt = parse_iso_arg("window_start", window_start)
    end_dt = parse_iso_arg("window_end", window_end)
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
            "coverage": "none",
            "slots": [],
            "summary": (
                f"None of {display_name}'s calendars ({labels}) are "
                f"shared with me, so I can't suggest free times. Ask "
                f"them to share at least one with "
                f"{assistant_email(ctx) or 'this assistant'} under "
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

    by_day = _day_breakdown(ctx, merged_busy, start_dt, end_dt)
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
            f"{humanize_iso(first['start'])} – "
            f"{humanize_iso(first['end'])}"
            + (
                f" (plus {len(slots) - 1} more option(s))."
                if len(slots) > 1
                else "."
            )
        )
    summary += _based_on(display_name, per_cal)

    out: Dict[str, Any] = {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": True,
        "coverage": _coverage(per_cal),
        "duration_minutes": duration_minutes,
        "working_hours_only": working_hours_only,
        "slots": slots,
    }
    if by_day:
        # Slots are the first openings in the window, not the lightest
        # day; this is what answers "which day is best?".
        out["days_least_to_most_busy"] = by_day["ranking"]
    out["summary"] = summary
    return out


# ---- calendar.create_event --------------------------------------------


CALENDAR_CREATE_EVENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Short event title shown on the calendar (e.g. "
                "'Hold', 'Dentist', 'Block — focus time'). Required."
            ),
        },
        "start": {
            "type": "string",
            "description": (
                "ISO 8601 start time WITH timezone offset (e.g. "
                "2026-04-21T14:00:00-04:00). The user almost always "
                "speaks in their local time — translate phrases like "
                "'next Tuesday at 2pm' into the offset that matches "
                "where the household lives."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "Optional ISO 8601 end time. If omitted, the event "
                "is created with a 60-minute duration starting at "
                "'start'. Must be strictly after start when supplied."
            ),
        },
        "person": {
            "type": "string",
            "description": (
                "Optional household member whose calendar gets the "
                "event. Defaults to the speaker themselves — the "
                "primary use case is 'add a hold on MY calendar'. "
                "Only the speaker may write to their own calendar; "
                "writes on someone else's calendar are refused even "
                "when the relationship would normally allow it."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "Optional longer note stored in the event body. "
                "Use this for context the user dictated ('the "
                "plumber is coming to look at the leak under the "
                "kitchen sink')."
            ),
        },
        "location": {
            "type": "string",
            "description": "Optional event location.",
        },
        "timezone": {
            "type": "string",
            "description": (
                "Optional IANA timezone name (e.g. "
                "'America/Los_Angeles'). Stored on the event so "
                "Google's local-time math is correct on the user's "
                "phone. Falls back to the offset embedded in 'start'."
            ),
        },
    },
    "required": ["summary", "start"],
}


def _default_event_end(start: datetime) -> datetime:
    """Default a missing end to start + 60 minutes — typical 'hold' length."""
    return start + timedelta(minutes=60)


async def handle_calendar_create_event(
    ctx: ToolContext,
    summary: str,
    start: str,
    end: Optional[str] = None,
    person: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    timezone: Optional[str] = None,  # noqa: A002 - matches schema
) -> Dict[str, Any]:
    """Create a Google calendar event on the speaker's own calendar.

    Use case: "add a hold on my calendar next Tuesday at 2pm". The
    LLM resolves the date/time to ISO 8601, picks a one-line
    summary, and calls this tool. Two consent gates must BOTH be
    satisfied or the call is refused with a clear, actionable
    error message:

    1. **Per-person consent** — the resolved subject's
       ``people.ai_can_write_calendar`` flag must be ``True``. The
       household member toggles this on themselves under their
       Person profile in the admin console; defaulting to ``False``
       keeps the existing read-only behaviour for households that
       don't opt in.
    2. **Speaker authorisation** — only the speaker may add events
       to their own calendar. Writing on someone else's calendar
       (even a spouse's) is refused for now: cross-person calendar
       writes have a much higher blast radius than reads, and we'd
       want a separate explicit consent for that case before
       enabling it.

    On success returns the event id, html link, and final
    start/end so the agent can confirm exactly what landed (and
    catch off-by-an-hour timezone mistakes early).
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    if ctx.person_id is None:
        raise ToolError(
            "I can't add a calendar event because I don't know who "
            "I'm talking to. Sign in via face recognition (live "
            "page) or use a registered email / Telegram account so "
            "I can attribute the request to you, then ask again."
        )

    if person:
        subject_row, calendar_pairs = resolve_person_calendars(ctx, person)
        if subject_row is None:
            raise ToolError(
                f"I couldn't find {person!r} in this household. Use "
                "lookup_person to find their exact name first."
            )
    else:
        subject_row = ctx.db.get(models.Person, ctx.person_id)
        if subject_row is None:
            raise ToolError(
                "Your speaker record was deleted mid-session. Please "
                "ask the family admin to re-add you, then try again."
            )
        calendar_pairs = calendar_pairs_for(subject_row)

    if subject_row.person_id != ctx.person_id:
        raise ToolError(
            f"I can only add events to your own calendar, not "
            f"{subject_row.first_name}'s. Ask {subject_row.first_name} "
            "to make the request themselves so they can confirm it."
        )

    if not bool(getattr(subject_row, "ai_can_write_calendar", False)):
        display_name = subject_row.preferred_name or subject_row.first_name
        raise ToolError(
            f"{display_name} hasn't given me permission to add "
            "events to their calendar yet. Open your Person profile "
            "in the admin console and toggle on 'Allow Avi to add "
            "calendar events', then ask again."
        )

    personal = next(
        (cid for cid, label in calendar_pairs if label == "personal"),
        None,
    )
    if not personal:
        raise ToolError(
            "I need a personal email address on your profile to "
            "know which calendar to write to. Add it under your "
            "Person profile and try again."
        )

    start_dt = parse_iso_arg("start", start)
    if end:
        end_dt = parse_iso_arg("end", end)
    else:
        end_dt = _default_event_end(start_dt)
    if end_dt <= start_dt:
        raise ToolError("end must be strictly after start.")

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    try:
        ev = await asyncio.to_thread(
            gcal_create_event,
            creds,
            calendar_id=personal,
            summary=summary,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
            timezone_name=timezone,
        )
    except CalendarNotShared as e:
        raise ToolError(
            f"I can't see {personal!r} at all — share that calendar "
            "with this assistant's Google account (Calendar → "
            "Settings → Share with specific people → Make changes "
            "to events) and try again."
        ) from e
    except CalendarReadOnly as e:
        raise ToolError(
            f"I can see {personal!r} but only with read access. Two "
            "things to check: (1) on the AI Assistant settings page, "
            "click Disconnect Google then Connect Google again so I "
            "get the new write scope; (2) re-share the calendar in "
            "Google Calendar with 'Make changes to events' instead "
            "of 'See all event details'."
        ) from e
    except CalendarError as e:
        raise ToolError(str(e)) from e

    return {
        "event_id": ev.event_id,
        "calendar_id": ev.calendar_id,
        "summary": ev.summary,
        "start": ev.start,
        "end": ev.end,
        "html_link": ev.html_link,
        "summary_text": (
            f"Added '{ev.summary}' to your calendar from "
            f"{humanize_iso(ev.start)} to {humanize_iso(ev.end)}."
        ),
    }


# ---- calendar.list_for_person -----------------------------------------


CALENDAR_LIST_FOR_PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member whose events "
                "you want to list." + _PERSON_ARG_HINT
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
    "required": ["window_start", "window_end"],
}


async def handle_calendar_list_for_person(
    ctx: ToolContext,
    window_start: str,
    window_end: str,
    person: Optional[str] = None,
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
    ask for the share. One shared as "see only free/busy" comes back
    with ``access="busy_times_only"``: its events have times but no
    titles, whoever is asking.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = resolve_calendar_subject(ctx, person)
    if person_row is None:
        # Direct-email lookup against a non-family calendar — refuse
        # to list events: we can't run the relationship gate without
        # a Person, and silently leaking detail would be wrong.
        raise ToolError(
            f"I can only list calendar events for registered family "
            f"members. {person!r} isn't one of them — try giving me "
            "their name as it appears in the admin console."
        )

    start_dt = parse_iso_arg("window_start", window_start)
    end_dt = parse_iso_arg("window_end", window_end)
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
    )
    show_detail = detail_decision.allowed
    display_name = person_row.preferred_name or person_row.first_name

    per_calendar_out: List[Dict[str, Any]] = []
    total_events = 0
    any_shared = False
    times_only: List[str] = []
    not_shared: List[str] = []

    for cal_id, label in calendar_pairs:
        try:
            window = await asyncio.to_thread(
                read_calendar_window,
                creds,
                calendar_id=cal_id,
                start=start_dt,
                end=end_dt,
                max_results=max_results_per_calendar,
            )
        except CalendarNotShared as e:
            not_shared.append(_calendar_name(label, cal_id))
            per_calendar_out.append(
                {
                    "calendar_id": cal_id,
                    "label": label,
                    "shared": False,
                    "access": "not_shared",
                    "reason": e.reason,
                }
            )
            continue
        except CalendarError as e:
            raise ToolError(str(e)) from e

        any_shared = True
        if not window.details_visible:
            times_only.append(_calendar_name(label, cal_id))
        rendered: List[Dict[str, Any]] = []
        for ev in window.events:
            if not window.details_visible:
                rendered.append(
                    {
                        "start": ev.start,
                        "end": ev.end,
                        "summary": "[busy — this calendar only shares times]",
                        "location": None,
                        "organizer_email": None,
                    }
                )
            elif show_detail:
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
                "access": (
                    "event_details"
                    if window.details_visible
                    else "busy_times_only"
                ),
                "events": rendered,
            }
        )

    if not any_shared:
        summary = (
            f"None of {display_name}'s calendars are shared with me. "
            f"Ask them to share at least one with "
            f"{assistant_email(ctx) or 'this assistant'}."
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
    if times_only:
        summary += (
            f" {display_name}'s {' and '.join(times_only)} only shares "
            "busy times with me, so I can say when those events are "
            "but not what they are."
        )
    if any_shared and not_shared:
        summary += (
            f" {display_name}'s {' and '.join(not_shared)} isn't shared "
            "with me, so anything on it is missing from this list."
        )

    return {
        "person": display_name,
        "show_detail": show_detail,
        "access_label": detail_decision.label,
        "per_calendar": per_calendar_out,
        "any_shared": any_shared,
        "coverage": (
            "none"
            if not any_shared
            else "partial"
            if not_shared
            else "complete"
        ),
        "total_events": total_events,
        "summary": summary,
    }
