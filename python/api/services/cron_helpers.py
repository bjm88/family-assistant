"""Cron-string helpers used by monitoring tasks.

Tiny wrapper around :pypi:`croniter` (the de-facto Python cron library)
and :pypi:`cron-descriptor` (English rendering of cron expressions). The
goal is to centralise:

* validation — "is this string a legal 5-field cron expression?"
* next-run computation — "given the family's IANA timezone, when does
  this expression next fire?"
* human description — "At 09:00 AM, every day"

so callers (the API router, the agent tool, the scheduler loop, the
monitoring tab) all parse the same way and can never disagree about
whether a string is valid.

Why centralise the timezone math
--------------------------------
Cron is timezone-sensitive: ``0 9 * * *`` means very different things
in UTC vs America/New_York vs Asia/Tokyo. Every call here takes an
explicit ``tz_name`` (no implicit "use server's timezone" default) so
the call site has to be deliberate. The scheduler stores
``next_run_at`` in **UTC** in the database; the UI converts back to
the family timezone for display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter
from cron_descriptor import (  # type: ignore[import-untyped]
    CasingTypeEnum,
    ExpressionDescriptor,
    Options,
)

logger = logging.getLogger(__name__)


# Standard 5-field cron expression (no seconds field, no aliases). We
# intentionally don't enable croniter's 6-field/7-field/aliases mode so
# the API surface matches what users expect from man 5 crontab and the
# cron-descriptor output stays predictable.
_CRON_FIELDS = 5


class CronError(ValueError):
    """Raised when a cron string fails parsing or validation.

    Wraps :class:`croniter.CroniterBadCronError` and the
    ``ZoneInfoNotFoundError`` from a bad timezone so callers can
    catch one type and turn it into a 422 Unprocessable Entity at the
    API boundary.
    """


@dataclass(frozen=True)
class CronInfo:
    """Validated cron expression + everything we computed from it."""

    expression: str
    timezone: str
    description: str
    next_run_utc: datetime


def parse(expression: str, tz_name: str) -> CronInfo:
    """Validate ``expression`` and compute its next firing in UTC.

    Raises :class:`CronError` on a bad expression or unknown timezone.
    The returned :class:`CronInfo` is safe to persist directly:
    ``next_run_utc`` is timezone-aware UTC, ready for the
    ``DateTime(timezone=True)`` column.
    """

    expr = (expression or "").strip()
    if not expr:
        raise CronError("Cron expression must not be empty.")

    if len(expr.split()) != _CRON_FIELDS:
        raise CronError(
            "Cron expression must have exactly 5 space-separated fields "
            "(minute hour day-of-month month day-of-week)."
        )

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise CronError(f"Unknown timezone: {tz_name!r}") from exc

    try:
        # croniter uses the supplied datetime's tzinfo to anchor the
        # iteration — passing a tz-aware "now" is what makes the
        # expression interpret in that local zone.
        now_local = datetime.now(tz)
        it = croniter(expr, now_local)
    except (CroniterBadCronError, ValueError) as exc:
        raise CronError(f"Invalid cron expression: {exc}") from exc

    # croniter returns a *naive* local datetime; lift it back to a UTC
    # tz-aware datetime for storage.
    next_local = it.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    next_utc = next_local.astimezone(timezone.utc)

    description = describe(expr)
    return CronInfo(
        expression=expr,
        timezone=tz_name,
        description=description,
        next_run_utc=next_utc,
    )


def next_run(expression: str, tz_name: str, *, after: Optional[datetime] = None) -> datetime:
    """Return the next firing of ``expression`` in UTC, after ``after``.

    Convenience wrapper for the scheduler tick — given the cron string
    and the moment a run just finished, when should the next one fire?
    """

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise CronError(f"Unknown timezone: {tz_name!r}") from exc

    if after is None:
        anchor = datetime.now(tz)
    else:
        # Accept naive UTC OR tz-aware; normalise to the family-local
        # time croniter wants.
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        anchor = after.astimezone(tz)

    try:
        it = croniter(expression, anchor)
    except (CroniterBadCronError, ValueError) as exc:
        raise CronError(f"Invalid cron expression: {exc}") from exc

    next_local = it.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc)


def describe(expression: str) -> str:
    """Render a cron expression as plain English.

    Uses :pypi:`cron-descriptor` (a pure-Python port of the .NET
    ``CronExpressionDescriptor``). Falls back to the raw expression if
    the library can't parse it — better than crashing the response.
    """

    expr = (expression or "").strip()
    if not expr:
        return ""
    try:
        opts = Options()
        opts.use_24hour_time_format = False
        # cron-descriptor's "Sentence" casing is the one that keeps the
        # AM/PM marker fully capitalised ("At 09:00 AM"); "Title"
        # title-cases every word and produces "At 09:00 Am".
        opts.casing_type = CasingTypeEnum.Sentence
        return ExpressionDescriptor(expr, opts).get_description()
    except Exception:  # noqa: BLE001 - never crash the response
        logger.warning(
            "cron_helpers.describe: failed to render %r in English", expr
        )
        return expr
