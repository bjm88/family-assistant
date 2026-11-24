""""When is Ben free?" — answered from the calendars Avi can actually read.

Avi's Google grant (``calendar.events``) cannot call Google's
``freebusy.query`` endpoint; Google answers 403 "insufficient
authentication scopes". Busy time is therefore worked out from each
calendar's events, which works at every share level — including a work
calendar shared as "see only free/busy".

These tests replace the Google client with a small fake that serves
canned ``events.list`` pages, and check that:

* an event blocks time exactly when Google Calendar would say it does;
* an un-shared calendar is reported as such without hiding the others;
* a missing permission on Avi's own connection is never described to
  the user as "share your calendar with me";
* the tools work out whose calendars a question is about (the speaker
  by default), say what Avi can see on each one, and add busy time up
  per day so "which day am I most free?" is answered with a date;
* every prompt tells the model which dates "next week" means.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from api import models
from api.ai import chat_prompts, fast_action_router, time_context
from api.ai.tools import ToolContext, ToolError
from api.ai.tools.handlers import calendar as cal_tools
from api.services import inbound_prompts
from api.integrations import google_calendar as gc
from api.integrations import google_oauth


NY = ZoneInfo("America/New_York")
WEEK_START = datetime(2026, 11, 30, 0, 0, tzinfo=NY)  # a Monday
WEEK_END = datetime(2026, 12, 5, 0, 0, tzinfo=NY)


def _http_error(status: int, message: str, *, reason: str | None = None) -> HttpError:
    error: dict[str, Any] = {"code": status, "message": message}
    if reason:
        error["details"] = [{"reason": reason}]
    return HttpError(
        SimpleNamespace(status=status, reason=message),
        json.dumps({"error": error}).encode(),
    )


SCOPE_403 = _http_error(
    403,
    "Request had insufficient authentication scopes.",
    reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT",
)


class FakeCalendarService:
    """Stands in for the Google client: ``events().list()`` and nothing else."""

    def __init__(self, calendars: dict[str, Any], roles: dict[str, str]):
        # calendar id -> list of pages (each a list of events), or an HttpError
        self.calendars = calendars
        # calendar id -> share level Google reports (default: writer)
        self.roles = roles
        self.list_calls: list[dict[str, Any]] = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return SimpleNamespace(execute=lambda: self._page(kwargs))

    def _page(self, kwargs):
        entry = self.calendars.get(kwargs["calendarId"])
        if entry is None:
            raise _http_error(404, "Not Found")
        if isinstance(entry, HttpError):
            raise entry
        index = int(kwargs.get("pageToken") or 0)
        page: dict[str, Any] = {
            "timeZone": "America/New_York",
            "accessRole": self.roles.get(kwargs["calendarId"], "writer"),
            "items": entry[index],
        }
        if index + 1 < len(entry):
            page["nextPageToken"] = str(index + 1)
        return page

    def freebusy(self):
        # The real endpoint refuses Avi's token; nothing may depend on it.
        return SimpleNamespace(
            query=lambda **_kw: SimpleNamespace(execute=lambda: (_ for _ in ()).throw(SCOPE_403))
        )


@pytest.fixture
def google(monkeypatch):
    """Install a fake Google Calendar; returns a function to load calendars."""
    holder: dict[str, FakeCalendarService] = {}

    def install(
        calendars: dict[str, Any], roles: dict[str, str] | None = None
    ) -> FakeCalendarService:
        holder["svc"] = FakeCalendarService(calendars, roles or {})
        return holder["svc"]

    monkeypatch.setattr(gc, "_service", lambda _creds: holder["svc"])
    monkeypatch.setattr(
        google_oauth, "load_credentials", lambda _db, _assistant_id: (object(), object())
    )
    return install


def _timed(day: int, start: str, end: str, **extra: Any) -> dict[str, Any]:
    """A timed event on December ``day`` (or Nov 30 when day == 30)."""
    month = 11 if day == 30 else 12
    return {
        "start": {"dateTime": f"2026-{month:02d}-{day:02d}T{start}:00-05:00"},
        "end": {"dateTime": f"2026-{month:02d}-{day:02d}T{end}:00-05:00"},
        **extra,
    }


def _busy(calendars, cal_id="ben@example.com"):
    return gc.freebusy(object(), start=WEEK_START, end=WEEK_END, calendar_ids=[cal_id])[cal_id]


def _spans(result) -> list[tuple[str, str]]:
    return [(b["start"][5:16], b["end"][5:16]) for b in result.busy]


# ---------------------------------------------------------------------------
# What counts as busy
# ---------------------------------------------------------------------------


def test_only_events_that_block_time_count_as_busy(google):
    google(
        {
            "ben@example.com": [
                [
                    _timed(1, "09:00", "10:00"),
                    _timed(1, "11:00", "12:00", status="cancelled"),
                    _timed(1, "13:00", "14:00", transparency="transparent"),
                    _timed(1, "15:00", "16:00", eventType="workingLocation"),
                    _timed(
                        1, "17:00", "18:00",
                        attendees=[{"self": True, "responseStatus": "declined"}],
                    ),
                    _timed(
                        2, "09:00", "09:30",
                        attendees=[
                            {"self": True, "responseStatus": "needsAction"},
                            {"responseStatus": "declined"},
                        ],
                    ),
                ]
            ]
        }
    )
    assert _spans(_busy(google)) == [
        ("12-01T09:00", "12-01T10:00"),
        ("12-02T09:00", "12-02T09:30"),
    ]


def test_overlapping_and_back_to_back_events_merge(google):
    google(
        {
            "ben@example.com": [
                [
                    _timed(1, "09:00", "10:00"),
                    _timed(1, "09:30", "11:00"),
                    _timed(1, "11:00", "11:30"),
                    _timed(1, "14:00", "15:00"),
                ]
            ]
        }
    )
    assert _spans(_busy(google)) == [
        ("12-01T09:00", "12-01T11:30"),
        ("12-01T14:00", "12-01T15:00"),
    ]


def test_all_day_busy_event_blocks_the_local_day(google):
    """An all-day "out of office" runs midnight to midnight where the calendar lives."""
    google(
        {
            "ben@example.com": [
                [{"start": {"date": "2026-12-02"}, "end": {"date": "2026-12-03"}}]
            ]
        }
    )
    assert [(b["start"], b["end"]) for b in _busy(google).busy] == [
        ("2026-12-02T00:00:00-05:00", "2026-12-03T00:00:00-05:00")
    ]


def test_events_are_clipped_to_the_window_and_reported_in_its_timezone(google):
    google(
        {
            "ben@example.com": [
                [
                    {  # started Sunday night, before the window opens
                        "start": {"dateTime": "2026-11-29T23:00:00-05:00"},
                        "end": {"dateTime": "2026-11-30T01:00:00-05:00"},
                    },
                    {  # stored in UTC by another client
                        "start": {"dateTime": "2026-12-01T15:00:00Z"},
                        "end": {"dateTime": "2026-12-01T16:00:00Z"},
                    },
                ]
            ]
        }
    )
    assert [(b["start"], b["end"]) for b in _busy(google).busy] == [
        ("2026-11-30T00:00:00-05:00", "2026-11-30T01:00:00-05:00"),
        ("2026-12-01T10:00:00-05:00", "2026-12-01T11:00:00-05:00"),
    ]


def test_a_busy_week_spanning_several_pages_is_read_in_full(google):
    svc = google(
        {
            "ben@example.com": [
                [_timed(1, "09:00", "10:00")],
                [_timed(2, "09:00", "10:00")],
                [_timed(3, "09:00", "10:00")],
            ]
        }
    )
    assert len(_busy(google).busy) == 3
    assert [c.get("pageToken") for c in svc.list_calls] == [None, "1", "2"]


def test_event_titles_are_not_requested_for_a_free_busy_question(google):
    svc = google({"ben@example.com": [[_timed(1, "09:00", "10:00")]]})
    _busy(google)
    fields = svc.list_calls[0]["fields"]
    assert "summary" not in fields and "location" not in fields and "description" not in fields


# ---------------------------------------------------------------------------
# What Avi can't see
# ---------------------------------------------------------------------------


def test_unshared_calendar_is_reported_without_hiding_the_shared_one(google):
    google(
        {
            "ben@example.com": [[_timed(1, "09:00", "10:00")]],
            "ben@locked.example": _http_error(403, "Forbidden"),
        }
    )
    per_cal = gc.busy_for_calendars(
        object(),
        calendars=[
            ("ben@example.com", "personal"),
            ("ben@locked.example", "work"),
            ("ben@nowhere.example", "work"),
        ],
        start=WEEK_START,
        end=WEEK_END,
    )
    assert [(b.label, b.shared, b.reason) for b in per_cal] == [
        ("personal", True, None),
        ("work", False, "forbidden"),
        ("work", False, "notFound"),
    ]
    assert len(gc.merge_busy_intervals(per_cal)) == 1


def test_missing_permission_on_avis_connection_is_not_blamed_on_sharing(google):
    """A scope 403 is Avi's Google grant, not the person's calendar."""
    google({"ben@example.com": SCOPE_403})
    with pytest.raises(gc.CalendarError) as err:
        _busy(google)
    message = str(err.value)
    assert "insufficient authentication scopes" in message
    assert "reconnect Google" in message
    assert "do not ask them to share a calendar" in message


# ---------------------------------------------------------------------------
# The tools Avi calls: whose calendars, what he can see, what it adds up to
# ---------------------------------------------------------------------------

WORK = "it.user@employer.example"


@pytest.fixture
def member_with_work_calendar(db, test_family):
    person = db.get(models.Person, test_family["person_id"])
    job = models.Job(person_id=person.person_id, work_email=WORK)
    db.add(job)
    db.commit()
    db.refresh(person)
    yield person
    db.delete(job)
    db.commit()


@pytest.fixture
def relatives(db, test_family):
    """Add family members with the given names; removed afterwards."""
    created: list[models.Person] = []

    def add(first: str, last: str, *, preferred: str | None = None, email: str | None = "auto"):
        tag = uuid.uuid4().hex[:8]
        person = models.Person(
            family_id=test_family["family_id"],
            first_name=first,
            preferred_name=preferred,
            last_name=last,
            email_address=f"{first.lower()}.{tag}@example.com" if email == "auto" else email,
        )
        db.add(person)
        db.commit()
        created.append(person)
        return person

    yield add
    for person in created:
        db.delete(person)
    db.commit()


def _ctx(db, test_family, *, speaker: Any = "member") -> ToolContext:
    return ToolContext(
        db=db,
        family_id=test_family["family_id"],
        assistant_id=test_family["assistant_id"],
        person_id=test_family["person_id"] if speaker == "member" else speaker,
    )


def _availability(ctx, start, end, **kwargs):
    return asyncio.run(
        cal_tools.handle_calendar_check_availability(ctx, start=start, end=end, **kwargs)
    )


NEXT_WEEK = ("2026-11-29T00:00:00-05:00", "2026-12-05T23:59:59-05:00")


def test_free_slots_avoid_both_personal_and_work_calendars(
    google, db, test_family, member_with_work_calendar
):
    """The question that failed first: "when am I free next week?"."""
    google(
        {
            test_family["person_email"]: [[_timed(30, "09:00", "10:00")]],
            WORK: [[_timed(30, "10:00", "12:30")]],
        },
        roles={WORK: "freeBusyReader"},
    )
    result = asyncio.run(
        cal_tools.handle_calendar_find_free_slots(
            _ctx(db, test_family),
            person="IntegrationTest",
            window_start="2026-11-30T00:00:00-05:00",
            window_end="2026-12-04T23:59:59-05:00",
            duration_minutes=60,
            max_slots=3,
        )
    )

    assert result["any_shared"] is True
    assert result["coverage"] == "complete"
    assert [s["start"][5:16] for s in result["slots"]] == [
        "11-30T12:30",
        "11-30T13:30",
        "11-30T14:30",
    ]
    assert {c["label"]: c["access"] for c in result["per_calendar"]} == {
        "personal": "event_details",
        "work": "busy_times_only",
    }
    assert "isn't shared" not in result["summary"]
    # The first openings are on the busiest day; the ranking is what
    # answers "which day is best?".
    assert result["days_least_to_most_busy"][0].startswith("Tuesday, Dec 1")
    assert result["days_least_to_most_busy"][-1] == "Monday, Nov 30: 3.5h busy"


def test_most_free_day_is_worked_out_per_date_across_both_calendars(
    google, db, test_family, member_with_work_calendar
):
    """A Sunday flight on the personal calendar and meetings on the work
    calendar: the lightest day is named with its date, and it is not the
    day of the flight."""
    google(
        {
            test_family["person_email"]: [
                [
                    {
                        "start": {"dateTime": "2026-11-29T13:00:00-05:00"},
                        "end": {"dateTime": "2026-11-29T20:30:00-05:00"},
                    },
                    _timed(3, "18:00", "19:00"),
                ]
            ],
            WORK: [
                [
                    _timed(30, "09:00", "17:00"),
                    _timed(1, "10:00", "11:00"),
                    _timed(2, "09:00", "14:30"),
                    _timed(3, "09:00", "12:00"),
                    _timed(4, "09:00", "16:00"),
                ]
            ],
        },
        roles={WORK: "freeBusyReader"},
    )
    result = _availability(_ctx(db, test_family), *NEXT_WEEK)

    assert [(d["weekday"], d["date"], d["busy_hours"]) for d in result["days"]] == [
        ("Sunday", "2026-11-29", 7.5),
        ("Monday", "2026-11-30", 8.0),
        ("Tuesday", "2026-12-01", 1.0),
        ("Wednesday", "2026-12-02", 5.5),
        ("Thursday", "2026-12-03", 4.0),
        ("Friday", "2026-12-04", 7.0),
        ("Saturday", "2026-12-05", 0.0),
    ]
    assert result["days"][0]["busy"] == [{"start": "13:00", "end": "20:30"}]
    assert result["days_least_to_most_busy"][:2] == [
        "Saturday, Dec 5: 0h busy",
        "Tuesday, Dec 1: 1h busy",
    ]
    assert "Nothing booked on Saturday, Dec 5." in result["summary"]
    assert "Most booked: Monday, Nov 30 (8h busy)." in result["summary"]
    assert result["coverage"] == "complete"
    assert (
        f"Based on IntegrationTest's personal calendar ({test_family['person_email']}) "
        f"and work calendar ({WORK}, busy times only)." in result["summary"]
    )


def test_an_overnight_event_counts_toward_both_days(google, db, test_family):
    google(
        {
            test_family["person_email"]: [
                [
                    {
                        "start": {"dateTime": "2026-12-01T22:00:00-05:00"},
                        "end": {"dateTime": "2026-12-02T06:00:00-05:00"},
                    }
                ]
            ]
        }
    )
    result = _availability(
        _ctx(db, test_family), "2026-12-01T00:00:00-05:00", "2026-12-02T23:59:00-05:00"
    )
    assert [(d["date"], d["busy_hours"], d["busy"]) for d in result["days"]] == [
        ("2026-12-01", 2.0, [{"start": "22:00", "end": "24:00"}]),
        ("2026-12-02", 6.0, [{"start": "00:00", "end": "06:00"}]),
    ]


def test_a_half_covered_first_day_is_not_ranked_against_whole_days(google, db, test_family):
    """Asked at 3pm about "the rest of the week": today's few remaining
    hours must not win "lightest day" just for being short."""
    google({test_family["person_email"]: [[_timed(2, "09:00", "10:00")]]})
    result = _availability(
        _ctx(db, test_family), "2026-12-01T15:00:00-05:00", "2026-12-03T23:59:59-05:00"
    )
    assert [d["partial_day"] for d in result["days"]] == [True, False, False]
    assert result["days_least_to_most_busy"] == [
        "Thursday, Dec 3: 0h busy",
        "Wednesday, Dec 2: 1h busy",
    ]


def test_a_question_about_one_afternoon_has_no_day_breakdown(google, db, test_family):
    google({test_family["person_email"]: [[_timed(1, "14:00", "15:00")]]})
    result = _availability(
        _ctx(db, test_family), "2026-12-01T13:00:00-05:00", "2026-12-01T17:00:00-05:00"
    )
    assert "days" not in result
    assert [(b["start"][11:16], b["end"][11:16]) for b in result["busy"]] == [("14:00", "15:00")]


def test_unshared_work_calendar_makes_the_answer_partial_and_says_so(
    google, db, test_family, member_with_work_calendar
):
    google({test_family["person_email"]: [[_timed(1, "14:00", "15:00")]]})
    result = _availability(
        _ctx(db, test_family), "2026-12-01T13:00:00-05:00", "2026-12-01T17:00:00-05:00"
    )

    assert result["coverage"] == "partial"
    assert {c["label"]: c["access"] for c in result["per_calendar"]} == {
        "personal": "event_details",
        "work": "not_shared",
    }
    assert f"work calendar ({WORK}) isn't shared with me" in result["summary"]
    assert "missing from this answer" in result["summary"]


def test_no_calendar_shared_at_all_is_reported_not_treated_as_free(google, db, test_family):
    google({})
    result = _availability(_ctx(db, test_family), *NEXT_WEEK)
    assert result["coverage"] == "none"
    assert result["any_shared"] is False
    assert "None of IntegrationTest's calendars are shared with me" in result["summary"]
    assert "days" not in result


# ---- whose calendar? ---------------------------------------------------


def test_my_calendar_means_the_person_who_is_asking(
    google, db, test_family, member_with_work_calendar
):
    svc = google({test_family["person_email"]: [[]], WORK: [[]]})
    for person in (None, "me", "I", "my"):
        svc.list_calls.clear()
        kwargs = {} if person is None else {"person": person}
        result = _availability(_ctx(db, test_family), *NEXT_WEEK, **kwargs)
        assert result["person"] == "IntegrationTest"
        assert [c["calendarId"] for c in svc.list_calls] == [test_family["person_email"], WORK]


def test_unidentified_speaker_asking_about_my_calendar_is_asked_who(google, db, test_family):
    google({})
    with pytest.raises(ToolError, match="don't know who I'm talking to"):
        _availability(_ctx(db, test_family, speaker=None), *NEXT_WEEK)


def test_people_are_found_by_nickname_full_name_or_possessive(google, db, test_family, relatives):
    zed = relatives("Zedekiah", "Quillfeather", preferred="Zed")
    relatives("Zedora", "Quillfeather")
    svc = google({zed.email_address: [[]]})

    for asked in ("Zed", "zedekiah", "Zedekiah Quillfeather", "Zed Quillfeather", "Zed's", "Zedek"):
        svc.list_calls.clear()
        result = _availability(_ctx(db, test_family), *NEXT_WEEK, person=asked)
        assert result["person"] == "Zed", asked
        assert [c["calendarId"] for c in svc.list_calls] == [zed.email_address], asked


def test_a_name_matching_two_people_is_not_guessed(google, db, test_family, relatives):
    relatives("Zedekiah", "Quillfeather")
    relatives("Zedora", "Quillfeather")
    svc = google({})
    for asked in ("Zed", "Quillfeather"):
        with pytest.raises(ToolError) as err:
            _availability(_ctx(db, test_family), *NEXT_WEEK, person=asked)
        assert "More than one family member matches" in str(err.value)
        assert "Zedekiah Quillfeather" in str(err.value) and "Zedora Quillfeather" in str(err.value)
    assert svc.list_calls == []


def test_unknown_name_is_reported_as_unknown_not_as_a_missing_email(google, db, test_family):
    google({})
    with pytest.raises(ToolError) as err:
        _availability(_ctx(db, test_family), *NEXT_WEEK, person="Xanthippe")
    assert "Nobody in this household is called 'Xanthippe'" in str(err.value)
    assert "IntegrationTest" in str(err.value)


def test_family_member_without_any_email_says_what_to_add(google, db, test_family, relatives):
    relatives("Zedekiah", "Quillfeather", email=None)
    google({})
    with pytest.raises(ToolError) as err:
        _availability(_ctx(db, test_family), *NEXT_WEEK, person="Zedekiah")
    assert "Zedekiah has no email address on file" in str(err.value)
    assert "work email under Jobs" in str(err.value)


def test_asking_by_work_email_still_checks_the_personal_calendar(
    google, db, test_family, member_with_work_calendar
):
    svc = google({test_family["person_email"]: [[]], WORK: [[]]})
    result = _availability(_ctx(db, test_family), *NEXT_WEEK, person=WORK.upper())
    assert result["person"] == "IntegrationTest"
    assert sorted(c["calendarId"] for c in svc.list_calls) == sorted(
        [test_family["person_email"], WORK]
    )


# ---- "what's on my calendar?" -----------------------------------------


def _listing(ctx, **kwargs):
    return asyncio.run(
        cal_tools.handle_calendar_list_for_person(
            ctx, window_start=NEXT_WEEK[0], window_end=NEXT_WEEK[1], **kwargs
        )
    )


def test_listing_a_free_busy_only_calendar_gives_times_and_says_why_no_titles(
    google, db, test_family, member_with_work_calendar
):
    google(
        {
            test_family["person_email"]: [[_timed(1, "18:00", "19:00", summary="Dinner")]],
            WORK: [[_timed(1, "09:00", "10:00")]],
        },
        roles={WORK: "freeBusyReader"},
    )
    result = _listing(_ctx(db, test_family))

    by_label = {c["label"]: c for c in result["per_calendar"]}
    assert by_label["personal"]["access"] == "event_details"
    assert [e["summary"] for e in by_label["personal"]["events"]] == ["Dinner"]
    assert by_label["work"]["access"] == "busy_times_only"
    assert [(e["start"][11:16], e["summary"]) for e in by_label["work"]["events"]] == [
        ("09:00", "[busy — this calendar only shares times]")
    ]
    assert result["coverage"] == "complete"
    assert f"work calendar ({WORK}) only shares busy times with me" in result["summary"]


def test_listing_with_a_missing_permission_is_not_blamed_on_sharing(google, db, test_family):
    google({test_family["person_email"]: SCOPE_403})
    with pytest.raises(ToolError) as err:
        _listing(_ctx(db, test_family))
    assert "reconnect Google" in str(err.value)


# ---------------------------------------------------------------------------
# Which dates "next week" means
# ---------------------------------------------------------------------------

NY_TUESDAY = datetime(2026, 11, 24, 16, 33, tzinfo=NY)


def test_prompt_spells_out_this_week_and_next_week_as_dates(db, test_family):
    block = time_context.render_time_context(db, test_family["family_id"], now=NY_TUESDAY)
    assert "Today: Tuesday 2026-11-24" in block
    assert "Tomorrow: Wednesday 2026-11-25" in block
    assert "This week: Sunday 2026-11-22 through Saturday 2026-11-28" in block
    assert "Next week: Sunday 2026-11-29 through Saturday 2026-12-05" in block
    assert "This weekend: Saturday 2026-11-28 and Sunday 2026-11-29" in block
    assert "give its date too" in block


def test_week_ranges_on_the_weekend_itself(db, test_family):
    saturday = time_context.render_time_context(
        db, test_family["family_id"], now=datetime(2026, 11, 28, 9, 0, tzinfo=NY)
    )
    assert "This week: Sunday 2026-11-22 through Saturday 2026-11-28" in saturday
    assert "This weekend: Saturday 2026-11-28 and Sunday 2026-11-29" in saturday

    sunday = time_context.render_time_context(
        db, test_family["family_id"], now=datetime(2026, 11, 29, 9, 0, tzinfo=NY)
    )
    assert "This week: Sunday 2026-11-29 through Saturday 2026-12-05" in sunday
    assert "This weekend: Saturday 2026-11-28 and Sunday 2026-11-29" in sunday


def test_today_is_the_households_date_not_the_servers(db, test_family):
    """11pm in New York is already tomorrow in UTC."""
    late = datetime(2026, 11, 25, 4, 0, tzinfo=ZoneInfo("UTC"))
    assert "Today: Tuesday 2026-11-24" in time_context.render_time_context(
        db, test_family["family_id"], now=late
    )


def test_week_can_start_on_monday():
    tuesday = NY_TUESDAY.date()
    assert time_context.week_start(tuesday, starts_on="monday").isoformat() == "2026-11-23"
    assert time_context.week_start(tuesday, starts_on="sunday").isoformat() == "2026-11-22"
    sunday = datetime(2026, 11, 29).date()
    assert time_context.week_start(sunday, starts_on="monday").isoformat() == "2026-11-23"
    assert time_context.week_start(sunday, starts_on="sunday").isoformat() == "2026-11-29"


def test_every_way_of_reaching_avi_gets_the_date_block(db, test_family):
    """Web chat, the quick-action path, and Slack / SMS / email / Telegram."""
    person = db.get(models.Person, test_family["person_id"])
    prompts = {
        "full agent": chat_prompts.build_system_prompt(
            db,
            assistant_name="Avi",
            family_name="Test",
            rag_block="",
            speaker_person_id=person.person_id,
            family_id=test_family["family_id"],
        ),
        "quick action": fast_action_router._build_fast_action_system_prompt(
            db,
            intent=fast_action_router.LABEL_CALENDAR_LIST,
            family_id=test_family["family_id"],
            speaker_person_id=person.person_id,
            surface_label="web chat",
        ),
        "inbound message": inbound_prompts.build_inbound_system_prompt(
            db,
            family_id=test_family["family_id"],
            person=person,
            surface_verb="chatting with on Slack",
            how_to_reply="--- How to reply ---\nKeep it short.",
        ),
    }
    for name, prompt in prompts.items():
        assert "--- Time / date context ---" in prompt, name
        assert "Next week: " in prompt, name
