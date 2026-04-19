"""Thin Google Calendar adapter.

Read-only Calendar helpers, all backed by the assistant's OAuth
credentials. The assistant sees:

* its own calendar (the Google account it was connected with), and
* any calendar a household member has *shared with* that account.

So once Ben shares his personal calendar with avi@…, every helper
below treats Ben's calendar like a first-class one — :func:`freebusy`
returns his busy intervals, :func:`find_free_slots` can carve out
windows that work for him, etc. If the calendar isn't shared the
freebusy response surfaces a per-calendar error which we propagate
to the caller as :class:`CalendarNotShared` so the LLM can give the
user a clear "ask Ben to share his calendar with me first" answer.

Public surface:

* :func:`list_calendar_ids` — every calendar the assistant can see.
* :func:`list_upcoming_events` — flat list of upcoming events sorted
  by start time, across one or many calendars.
* :func:`freebusy` — raw Google freebusy result, busy + per-calendar
  errors split.
* :func:`busy_for_calendar` — convenience: "is this one calendar
  busy in this window?" returning either intervals or
  :class:`CalendarNotShared`.
* :func:`find_free_slots` — given busy intervals + a window + a
  duration, return suggested free slots (optionally clamped to a
  daily working-hours band).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Sequence

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class CalendarError(RuntimeError):
    """Raised when Calendar rejects a request (auth, scope, quota)."""


class CalendarNotShared(CalendarError):
    """Raised when the requested calendar isn't visible to the assistant.

    Distinct from generic :class:`CalendarError` so the agent can
    convert it into a user-friendly "please share your calendar with
    Avi" response instead of a generic 4xx surface.
    """

    def __init__(self, calendar_id: str, reason: str = "notFound") -> None:
        super().__init__(
            f"Calendar {calendar_id!r} is not shared with this assistant "
            f"(reason: {reason})."
        )
        self.calendar_id = calendar_id
        self.reason = reason


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    calendar_id: str
    summary: str
    start: str  # RFC3339; date-only for all-day events
    end: str
    location: Optional[str] = None
    organizer_email: Optional[str] = None


def _service(creds: Credentials):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_calendar_ids(creds: Credentials) -> List[str]:
    """Return every calendar id visible to the assistant.

    Includes calendars the assistant owns *and* anything shared with
    them (which is the whole point of free/busy on the family schedule).
    """
    try:
        svc = _service(creds)
        resp = svc.calendarList().list().execute()
    except HttpError as exc:
        raise CalendarError(_summarise_http_error(exc)) from exc
    return [
        item["id"]
        for item in resp.get("items", [])
        if item.get("id")
    ]


def list_upcoming_events(
    creds: Credentials,
    *,
    hours_ahead: int = 24,
    max_results: int = 25,
    calendar_ids: Optional[Sequence[str]] = None,
) -> List[CalendarEvent]:
    """Return events starting in the next ``hours_ahead`` hours.

    Pulls from every readable calendar by default. Skips cancelled
    events. Sorted ascending by start time.
    """
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=hours_ahead)).isoformat()

    targets = list(calendar_ids) if calendar_ids else list_calendar_ids(creds)

    out: List[CalendarEvent] = []
    try:
        svc = _service(creds)
        for cal_id in targets:
            resp = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=max_results,
                    showDeleted=False,
                )
                .execute()
            )
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                start = ev.get("start", {})
                end = ev.get("end", {})
                organizer = ev.get("organizer", {}) or {}
                out.append(
                    CalendarEvent(
                        event_id=ev.get("id", ""),
                        calendar_id=cal_id,
                        summary=ev.get("summary", "(no title)"),
                        start=start.get("dateTime") or start.get("date") or "",
                        end=end.get("dateTime") or end.get("date") or "",
                        location=ev.get("location"),
                        organizer_email=organizer.get("email"),
                    )
                )
    except HttpError as exc:
        raise CalendarError(_summarise_http_error(exc)) from exc

    out.sort(key=lambda e: e.start)
    return out[:max_results]


@dataclass(frozen=True)
class FreeBusyResult:
    """Per-calendar freebusy outcome.

    ``busy`` is the list of ``{"start": iso, "end": iso}`` intervals
    Google reports for that calendar in the requested window.
    ``errors`` is the list of per-calendar errors Google attaches
    when the assistant doesn't have access (e.g. ``notFound`` for an
    un-shared calendar). The two lists are mutually exclusive in
    practice; we keep both in the dataclass so callers can decide how
    to render either case.
    """

    busy: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)

    @property
    def shared(self) -> bool:
        """True iff the calendar was visible to the assistant.

        Empty ``busy`` + empty ``errors`` is "shared but free" —
        still treated as shared.
        """
        return not self.errors


def freebusy(
    creds: Credentials,
    *,
    start: datetime,
    end: datetime,
    calendar_ids: Optional[Sequence[str]] = None,
) -> dict[str, FreeBusyResult]:
    """Return Google's freebusy structure for the requested window.

    The shape is ``{calendar_id: FreeBusyResult(busy=[...], errors=[...])}``.
    A calendar that isn't shared with the assistant comes back with a
    non-empty ``errors`` list (Google reports e.g.
    ``[{"domain": "global", "reason": "notFound"}]``). Callers can
    use :attr:`FreeBusyResult.shared` to branch on that without
    re-parsing the dict.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    targets = list(calendar_ids) if calendar_ids else list_calendar_ids(creds)
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": cid} for cid in targets],
    }
    try:
        svc = _service(creds)
        resp = svc.freebusy().query(body=body).execute()
    except HttpError as exc:
        raise CalendarError(_summarise_http_error(exc)) from exc
    out: dict[str, FreeBusyResult] = {}
    for cid, bucket in (resp.get("calendars") or {}).items():
        out[cid] = FreeBusyResult(
            busy=list(bucket.get("busy") or []),
            errors=list(bucket.get("errors") or []),
        )
    return out


def busy_for_calendar(
    creds: Credentials,
    *,
    calendar_id: str,
    start: datetime,
    end: datetime,
) -> List[dict]:
    """Return ``[{start,end}, …]`` for a single calendar.

    Raises :class:`CalendarNotShared` if the assistant can't see that
    calendar (the typical "user hasn't shared it with avi@…" case).
    Empty list means "shared and entirely free in this window".
    """
    results = freebusy(
        creds, start=start, end=end, calendar_ids=[calendar_id]
    )
    bucket = results.get(calendar_id)
    if bucket is None or not bucket.shared:
        reason = (bucket.errors[0].get("reason") if bucket and bucket.errors else "notFound")
        raise CalendarNotShared(calendar_id, reason=reason)
    return bucket.busy


@dataclass(frozen=True)
class PerCalendarBusy:
    """Outcome for ONE calendar id when querying multiple at once.

    The agent uses this to:

    * tell the user which of the person's calendars (personal /
      work) returned which busy intervals,
    * report ``shared=False`` for any calendar the person has on
      file but hasn't shared with Avi yet (so the LLM can prompt
      them to share that specific one), and
    * still merge the busy intervals into a single answer for the
      simple "is X free?" use case.
    """

    calendar_id: str
    label: str  # e.g. "personal", "work"
    shared: bool
    busy: List[dict] = field(default_factory=list)
    reason: Optional[str] = None  # filled when shared=False


def busy_for_calendars(
    creds: Credentials,
    *,
    calendars: Sequence[tuple[str, str]],
    start: datetime,
    end: datetime,
) -> List[PerCalendarBusy]:
    """Run a single freebusy query against one or more (calendar_id, label) pairs.

    ``calendars`` is a list of ``(calendar_id, label)`` tuples. The
    label is opaque to Google — we just round-trip it for the caller
    so the agent can render "personal" vs "work" without an extra
    lookup. Per-calendar visibility is reported individually:

    * ``shared=True``  — calendar visible, ``busy`` may be empty.
    * ``shared=False`` — calendar not shared with the assistant;
      ``reason`` carries Google's error code (typically ``notFound``).

    Returns one :class:`PerCalendarBusy` per input calendar in the
    SAME order as the input. Duplicate calendar ids (e.g. if a person
    has accidentally typed the same address into both fields) are
    de-duped on the wire but echoed back once per input slot so the
    caller's output is symmetrical with its input.
    """
    if not calendars:
        return []

    unique_ids: List[str] = []
    seen: set[str] = set()
    for cid, _label in calendars:
        if cid and cid not in seen:
            unique_ids.append(cid)
            seen.add(cid)

    if not unique_ids:
        return [
            PerCalendarBusy(
                calendar_id=cid,
                label=label,
                shared=False,
                reason="empty_calendar_id",
            )
            for cid, label in calendars
        ]

    results = freebusy(creds, start=start, end=end, calendar_ids=unique_ids)
    out: List[PerCalendarBusy] = []
    for cid, label in calendars:
        bucket = results.get(cid)
        if bucket is None:
            out.append(
                PerCalendarBusy(
                    calendar_id=cid,
                    label=label,
                    shared=False,
                    reason="not_returned",
                )
            )
            continue
        if not bucket.shared:
            reason = (
                bucket.errors[0].get("reason")
                if bucket.errors
                else "notFound"
            )
            out.append(
                PerCalendarBusy(
                    calendar_id=cid,
                    label=label,
                    shared=False,
                    reason=reason,
                )
            )
            continue
        out.append(
            PerCalendarBusy(
                calendar_id=cid,
                label=label,
                shared=True,
                busy=list(bucket.busy),
            )
        )
    return out


def merge_busy_intervals(per_calendar: Sequence[PerCalendarBusy]) -> List[dict]:
    """Union the busy intervals across multiple calendars.

    Returns a sorted, merged list of ``{"start": iso, "end": iso}``
    intervals — the input to :func:`find_free_slots` for the
    "across all of Ben's calendars" case. Calendars that came back
    ``shared=False`` are skipped silently (the caller should surface
    that separately in the user-facing reply).
    """
    intervals: List[tuple[datetime, datetime]] = []
    for bucket in per_calendar:
        if not bucket.shared:
            continue
        for b in bucket.busy:
            try:
                bs = _parse_iso(b["start"])
                be = _parse_iso(b["end"])
            except (KeyError, ValueError):
                continue
            if be > bs:
                intervals.append((bs, be))
    intervals.sort()
    merged: List[tuple[datetime, datetime]] = []
    for bs, be in intervals:
        if merged and bs <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], be))
        else:
            merged.append((bs, be))
    return [
        {"start": bs.isoformat(), "end": be.isoformat()} for bs, be in merged
    ]


def events_for_calendar(
    creds: Credentials,
    *,
    calendar_id: str,
    start: datetime,
    end: datetime,
    max_results: int = 50,
) -> List[CalendarEvent]:
    """Read events on ONE calendar between ``start`` and ``end``.

    Sister of :func:`list_upcoming_events` but window-driven and
    single-calendar. Raises :class:`CalendarNotShared` when Google
    refuses access (so the agent can surface a clean "ask them to
    share their calendar" message). Empty list means "shared and
    nothing on the calendar in that window".
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    try:
        svc = _service(creds)
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
                showDeleted=False,
            )
            .execute()
        )
    except HttpError as exc:
        # Google returns 404 for both "calendar doesn't exist" and
        # "we never shared this with you" — treat both as
        # CalendarNotShared so the agent's user-facing copy stays
        # consistent.
        status = getattr(exc.resp, "status", None) if exc.resp else None
        if status in (403, 404):
            raise CalendarNotShared(
                calendar_id, reason=f"http_{status}"
            ) from exc
        raise CalendarError(_summarise_http_error(exc)) from exc

    out: List[CalendarEvent] = []
    for ev in resp.get("items", []):
        if ev.get("status") == "cancelled":
            continue
        s = ev.get("start", {})
        e = ev.get("end", {})
        organizer = ev.get("organizer", {}) or {}
        out.append(
            CalendarEvent(
                event_id=ev.get("id", ""),
                calendar_id=calendar_id,
                summary=ev.get("summary", "(no title)"),
                start=s.get("dateTime") or s.get("date") or "",
                end=e.get("dateTime") or e.get("date") or "",
                location=ev.get("location"),
                organizer_email=organizer.get("email"),
            )
        )
    out.sort(key=lambda x: x.start)
    return out


def find_free_slots(
    *,
    busy: Sequence[dict],
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int = 30,
    working_hours: Optional[tuple[int, int]] = (9, 18),
    max_slots: int = 5,
    tz: timezone = timezone.utc,
) -> List[dict]:
    """Carve free slots out of a busy list.

    ``busy`` is the list returned by :func:`busy_for_calendar`.
    ``working_hours`` clamps each candidate slot to the given
    ``(start_hour, end_hour)`` band per local day — pass ``None`` to
    consider the entire 24h window. The returned slots are
    ``{"start": iso, "end": iso}`` aligned to the next 15-minute
    boundary so suggestions feel natural ("3:00, 3:15, 3:30").

    The implementation is intentionally pure (no API calls) so we
    can unit-test it cheaply. The agent calls
    :func:`busy_for_calendar` first, then funnels its output here.
    """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    # Normalise busy intervals to datetimes in the requested tz, drop
    # anything fully outside the window, then merge overlaps so we
    # don't have to handle them in the slot search.
    intervals: List[tuple[datetime, datetime]] = []
    for b in busy:
        try:
            bs = _parse_iso(b["start"]).astimezone(tz)
            be = _parse_iso(b["end"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if be <= window_start or bs >= window_end:
            continue
        intervals.append((max(bs, window_start), min(be, window_end)))
    intervals.sort()
    merged: List[tuple[datetime, datetime]] = []
    for bs, be in intervals:
        if merged and bs <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], be))
        else:
            merged.append((bs, be))

    duration = timedelta(minutes=int(duration_minutes))
    quarter = timedelta(minutes=15)

    def _round_up(t: datetime) -> datetime:
        # Align to the next 15-minute mark so suggestions look human.
        minutes = (t.minute // 15) * 15
        rounded = t.replace(minute=minutes, second=0, microsecond=0)
        if rounded < t:
            rounded += quarter
        return rounded

    def _in_working_hours(slot_start: datetime, slot_end: datetime) -> bool:
        if working_hours is None:
            return True
        start_h, end_h = working_hours
        local_start = slot_start.astimezone(tz)
        local_end = slot_end.astimezone(tz)
        # Same calendar day + inside band
        if local_start.date() != local_end.date():
            return False
        if local_start.time() < time(start_h, 0):
            return False
        if local_end.time() > time(end_h, 0):
            return False
        return True

    cursor = _round_up(window_start.astimezone(tz))
    out: List[dict] = []

    def _try_emit_until(barrier: datetime) -> None:
        nonlocal cursor
        while len(out) < max_slots:
            slot_start = cursor
            slot_end = slot_start + duration
            if slot_end > barrier or slot_end > window_end:
                return
            if _in_working_hours(slot_start, slot_end):
                out.append(
                    {
                        "start": slot_start.isoformat(),
                        "end": slot_end.isoformat(),
                    }
                )
                cursor = slot_end
            else:
                # Jump to next day's working-hours band.
                next_day = (slot_start + timedelta(days=1)).replace(
                    hour=working_hours[0] if working_hours else 0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                if next_day >= barrier:
                    return
                cursor = next_day

    for bs, be in merged:
        _try_emit_until(bs)
        if len(out) >= max_slots:
            break
        cursor = max(cursor, be)
        cursor = _round_up(cursor)

    if len(out) < max_slots:
        _try_emit_until(window_end)

    return out


def _parse_iso(value: str) -> datetime:
    """Parse Google's RFC3339 timestamps (handles trailing 'Z')."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _summarise_http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None) if exc.resp else None
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(exc)
    except Exception:  # noqa: BLE001 - raw fallback
        message = str(exc)
    return f"Calendar HTTP {status}: {message}"
