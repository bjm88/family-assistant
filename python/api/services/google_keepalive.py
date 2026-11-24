"""Keep Avi's Google connection alive and shout when it dies.

Every ``GOOGLE_KEEPALIVE_INTERVAL_S`` (hourly by default) the backend
force-refreshes each assistant's Google access token and verifies it
with Google's tokeninfo endpoint. That:

* keeps the refresh token in use, so Google's ~6-month inactivity
  expiry never applies;
* notices a revoked / expired grant within the hour instead of on the
  next Gmail send or Sunday backup;
* reads ``refresh_token_expires_in``, which Google only sends when the
  grant has a fixed lifetime (OAuth app still in "Testing" = 7 days).

What it cannot do: extend a fixed-lifetime grant. When the token is
dead or about to die, the household's parents get one Telegram (or
SMS) message telling them to reconnect, rather than finding out weeks
later from a missing backup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from .. import models
from ..ai import authz
from ..config import get_settings
from ..db import SessionLocal
from ..integrations import google_oauth, telegram, twilio_sms

logger = logging.getLogger(__name__)

# Warn this far ahead when Google says the refresh token has an end date.
_EXPIRY_WARNING = timedelta(days=2)
_STARTUP_DELAY_S = 60.0


@dataclass
class KeepaliveState:
    last_checked_at: Optional[datetime] = None
    last_ok_at: Optional[datetime] = None
    last_error: Optional[str] = None
    revoked: bool = False
    refresh_token_expires_at: Optional[datetime] = None
    # Alerts already sent for the current problem, so an hourly loop
    # doesn't text the parents every hour. Cleared on recovery.
    alerted: set[str] = field(default_factory=set)


_state: dict[int, KeepaliveState] = {}


def keepalive_state() -> dict[int, KeepaliveState]:
    return _state


def run_once() -> dict[int, KeepaliveState]:
    """Refresh + verify every connected assistant. Never raises."""
    with SessionLocal() as db:
        rows = db.execute(select(models.GoogleOAuthCredential)).scalars().all()
        for row in rows:
            _check_assistant(db, row)
    return _state


def _check_assistant(db, row: models.GoogleOAuthCredential) -> None:
    aid = row.assistant_id
    state = _state.setdefault(aid, KeepaliveState())
    now = datetime.now(timezone.utc)
    state.last_checked_at = now
    try:
        result = google_oauth.force_refresh(db, aid)
        db.commit()
    except google_oauth.GoogleRefreshRevoked as exc:
        db.rollback()
        state.revoked = True
        state.last_error = str(exc)
        logger.error("google_keepalive: assistant %s grant is dead: %s", aid, exc)
        _alert_once(
            db,
            row,
            state,
            "revoked",
            f"Avi lost access to Google ({row.granted_email}): Gmail, Calendar "
            f"and Drive backups are offline. Google said: {exc}. "
            f"Reconnect here: {_reconnect_url(db, aid)}",
        )
        return
    except Exception as exc:  # noqa: BLE001 - transient; next hour retries
        db.rollback()
        state.last_error = str(exc)
        logger.warning("google_keepalive: assistant %s refresh failed: %s", aid, exc)
        return

    recovered = state.revoked
    state.revoked = False
    state.last_error = None
    state.last_ok_at = now
    state.refresh_token_expires_at = result.refresh_token_expires_at
    if recovered:
        state.alerted.clear()
    logger.info(
        "google_keepalive: assistant %s ok (%s), access token valid until %s%s",
        aid,
        result.email or row.granted_email,
        result.access_expires_at.isoformat(timespec="minutes"),
        (
            f", refresh token expires {result.refresh_token_expires_at:%Y-%m-%d %H:%M} UTC"
            if result.refresh_token_expires_at
            else ""
        ),
    )

    expires = result.refresh_token_expires_at
    if expires is not None and expires - now < _EXPIRY_WARNING:
        _alert_once(
            db,
            row,
            state,
            f"expiring:{expires.date()}",
            f"Avi's Google connection ({row.granted_email}) expires "
            f"{expires:%a %b %-d at %H:%M} UTC. Google gives it a fixed "
            "lifetime because the OAuth app is in Testing mode; publish it "
            "(Google Cloud console > OAuth consent screen > Publish app) and "
            f"reconnect once so it stops expiring: {_reconnect_url(db, aid)}",
        )


def _reconnect_url(db, assistant_id: int) -> str:
    assistant = db.get(models.Assistant, assistant_id)
    base = (get_settings().PUBLIC_BASE_URL or "http://localhost:5173").rstrip("/")
    family = assistant.family_id if assistant else ""
    return f"{base}/admin/families/{family}/assistant"


def _alert_once(db, row, state: KeepaliveState, key: str, text: str) -> None:
    if key in state.alerted:
        return
    if _alert_parents(db, row.assistant_id, text):
        state.alerted.add(key)


def _alert_parents(db, assistant_id: int, text: str) -> bool:
    """Telegram each primary parent, falling back to SMS. True if any landed."""
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        return False
    settings = get_settings()
    people = db.execute(
        select(models.Person).where(models.Person.family_id == assistant.family_id)
    ).scalars().all()
    delivered = False
    for person in (p for p in people if authz.is_primary_parent(p)):
        if person.telegram_user_id and settings.TELEGRAM_BOT_TOKEN:
            try:
                telegram.send_message(
                    bot_token=settings.TELEGRAM_BOT_TOKEN,
                    chat_id=person.telegram_user_id,
                    text=text,
                )
                delivered = True
                continue
            except telegram.TelegramSendError:
                logger.exception("google_keepalive: Telegram alert failed")
        if (
            person.mobile_phone_number
            and settings.TWILIO_ACCOUNT_SID
            and settings.TWILIO_PRIMARY_PHONE
        ):
            try:
                twilio_sms.send_sms(
                    account_sid=settings.TWILIO_ACCOUNT_SID,
                    auth_token=settings.TWILIO_AUTH_TOKEN or "",
                    from_phone=settings.TWILIO_PRIMARY_PHONE,
                    to_phone=person.mobile_phone_number,
                    body=text,
                    messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                )
                delivered = True
            except twilio_sms.TwilioSendError:
                logger.exception("google_keepalive: SMS alert failed")
    if not delivered:
        logger.error("google_keepalive: no parent could be alerted: %s", text)
    return delivered


async def run_google_keepalive_loop(stop_event: asyncio.Event) -> None:
    interval_s = max(300.0, get_settings().GOOGLE_KEEPALIVE_INTERVAL_S)
    delay = _STARTUP_DELAY_S
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(run_once)
        except Exception:  # noqa: BLE001 - the loop must survive anything
            logger.exception("google_keepalive: run crashed; retrying next interval")
        delay = interval_s
