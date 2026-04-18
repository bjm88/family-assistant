"""Thin Google Calendar adapter.

Two operations exposed today, both read-only and both work against
calendars *shared with* the assistant's account (so once the user
shares their personal calendar with avi@…, freebusy and event lookups
return both):

* :func:`list_upcoming_events` — flat list of upcoming events sorted
  by start time, across one or many calendars.
* :func:`freebusy` — Google's freebusy endpoint, useful for "is the
  user free Saturday morning?" answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class CalendarError(RuntimeError):
    """Raised when Calendar rejects a request (auth, scope, quota)."""


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


def freebusy(
    creds: Credentials,
    *,
    start: datetime,
    end: datetime,
    calendar_ids: Optional[Sequence[str]] = None,
) -> dict:
    """Return Google's freebusy structure for the requested window.

    The shape is ``{calendar_id: [{"start": ISO, "end": ISO}, …]}`` —
    one bucket per requested calendar, each bucket a list of busy
    intervals. Empty list means "free for the whole window".
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
    return {
        cid: bucket.get("busy", [])
        for cid, bucket in (resp.get("calendars") or {}).items()
    }


def _summarise_http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None) if exc.resp else None
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(exc)
    except Exception:  # noqa: BLE001 - raw fallback
        message = str(exc)
    return f"Calendar HTTP {status}: {message}"
