"""Catch Avi up on mail and messages that arrived while it was down.

One function, two callers:

* The FastAPI lifespan runs it once before the live email and Telegram
  pollers start, so a power-outage backlog is handled on the way up.
* ``scripts/recover_inbound.py`` runs the same function on demand.

Email uses Gmail's unread inbox (anything we didn't mark read is still
waiting). SMS and WhatsApp use Twilio's message list, because a webhook
Twilio gave up on never becomes "unread" on our side. Telegram uses a
short ``getUpdates`` drain of whatever the Bot API is still holding.

The live pollers take the same advisory lock, so a batch run and a
poller tick cannot both reply to one message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from ..config import get_settings
from . import email_inbox, inbound_lock, sms_inbox, telegram_inbox


logger = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    """What one catch-up pass examined. Counts include ignored senders."""

    emails_examined: int = 0
    messages_replayed: int = 0
    telegram_updates: int = 0
    errors: List[str] = field(default_factory=list)
    disabled: bool = False

    def summary(self) -> str:
        if self.disabled:
            return "Inbound recovery is disabled (AI_INBOUND_RECOVERY_ENABLED=false)."
        text = (
            f"Inbound recovery: {self.emails_examined} email(s), "
            f"{self.messages_replayed} SMS/WhatsApp message(s), "
            f"{self.telegram_updates} Telegram update(s)."
        )
        if self.errors:
            text += " Errors: " + "; ".join(self.errors)
        return text


def recover_missed_inbound() -> RecoveryReport:
    """Review unread email and missed messages, and process each one.

    Safe to call while the assistant is already running: the recovery
    lock serialises this pass with the live pollers. Per-channel
    failures are recorded on the report and do not skip the other
    channels.
    """
    settings = get_settings()
    if not settings.AI_INBOUND_RECOVERY_ENABLED:
        logger.info(
            "Inbound recovery skipped (AI_INBOUND_RECOVERY_ENABLED=false)."
        )
        return RecoveryReport(disabled=True)

    report = RecoveryReport()
    logger.info(
        "Inbound recovery starting (max_emails=%d, max_messages=%d, sms_lookback=%dh)",
        settings.AI_INBOUND_RECOVERY_MAX_EMAILS,
        settings.AI_INBOUND_RECOVERY_MAX_MESSAGES,
        settings.AI_INBOUND_RECOVERY_SMS_LOOKBACK_HOURS,
    )
    with inbound_lock.hold_recovery_lock():
        try:
            report.emails_examined = email_inbox.drain_unread_inboxes(
                max_messages=settings.AI_INBOUND_RECOVERY_MAX_EMAILS
            )
        except Exception as exc:  # noqa: BLE001 - keep going to the other channels
            logger.exception("Inbound recovery: email drain failed")
            report.errors.append(f"email: {exc}")

        try:
            report.messages_replayed = sms_inbox.replay_missed_messages(
                lookback_hours=settings.AI_INBOUND_RECOVERY_SMS_LOOKBACK_HOURS,
                max_messages=settings.AI_INBOUND_RECOVERY_MAX_MESSAGES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inbound recovery: SMS/WhatsApp replay failed")
            report.errors.append(f"sms: {exc}")

        try:
            report.telegram_updates = telegram_inbox.drain_pending_updates(
                max_updates=settings.AI_INBOUND_RECOVERY_MAX_MESSAGES
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inbound recovery: Telegram drain failed")
            report.errors.append(f"telegram: {exc}")

    logger.info(report.summary())
    return report
