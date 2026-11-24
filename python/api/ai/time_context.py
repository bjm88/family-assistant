"""The "what day is it" block every agent prompt carries.

A model has no clock. Without this block it cannot turn "tomorrow" or
"next week" into dates at all, and with only today's date it still has
to do calendar arithmetic it routinely gets wrong. So the block spells
out the ranges people actually ask about — this week, next week, this
weekend — as concrete dates, using the household's timezone and its
first day of the week.

It also tells the model to say the date whenever it names a weekday:
"you're most free Sunday" is useless when one Sunday has a flight on
it and the next one is empty.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings


DEFAULT_TIMEZONE = "America/New_York"


def family_timezone(db: Session, family_id: Optional[int]) -> ZoneInfo:
    """The household's timezone, falling back to US Eastern."""
    name = None
    if family_id is not None:
        family = db.get(models.Family, family_id)
        name = getattr(family, "timezone", None) if family is not None else None
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - unknown zone name on the family row
        return ZoneInfo(DEFAULT_TIMEZONE)


def week_start(today: date, *, starts_on: Optional[str] = None) -> date:
    """First day of the week containing ``today``.

    ``starts_on`` is ``"sunday"`` or ``"monday"``; defaults to the
    ``WEEK_STARTS_ON`` setting.
    """
    if starts_on is None:
        starts_on = get_settings().WEEK_STARTS_ON
    if str(starts_on).strip().lower() == "monday":
        return today - timedelta(days=today.weekday())
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _day(d: date) -> str:
    return d.strftime("%A %Y-%m-%d")


def render_time_context(
    db: Session,
    family_id: Optional[int],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Render the time / date block for a system prompt."""
    tz = family_timezone(db, family_id)
    now_local = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = now_local.date()

    this_week = week_start(today)
    next_week = this_week + timedelta(days=7)
    # The coming Saturday and the Sunday after it; on a Sunday, the
    # weekend already under way.
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 6:
        saturday = today - timedelta(days=1)

    return (
        "--- Time / date context ---\n"
        f"Family timezone: {tz.key}\n"
        f"Current local datetime: {now_local.isoformat(timespec='minutes')}\n"
        f"Today: {_day(today)}\n"
        f"Tomorrow: {_day(today + timedelta(days=1))}\n"
        f"This week: {_day(this_week)} through "
        f"{_day(this_week + timedelta(days=6))}\n"
        f"Next week: {_day(next_week)} through "
        f"{_day(next_week + timedelta(days=6))}\n"
        f"This weekend: {_day(saturday)} and "
        f"{_day(saturday + timedelta(days=1))}\n"
        "Use exactly these dates when the user says 'this week', 'next "
        "week' or 'this weekend'; do not work the ranges out yourself. "
        "Translate 'tomorrow' / 'next Monday' / '10am' to ISO 8601 with "
        "the family timezone offset before calling a tool.\n"
        "Whenever your reply names a day of the week, give its date too "
        "('Sunday, Dec 6'), so nobody has to guess which Sunday you mean."
    )


__all__ = ["family_timezone", "render_time_context", "week_start"]
