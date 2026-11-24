"""Downtime catch-up: unread email and missed messages share one pass.

The live poller only nibbles a few unread emails per tick. Recovery
is the pass that drains the backlog, and both startup and
``scripts/recover_inbound.py`` call it. These tests lock that
orchestration in without talking to Gmail, Twilio, or Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api.integrations import twilio_sms
from api.services import email_inbox, inbound_recovery


def test_recover_runs_email_sms_and_telegram():
    """One call reviews all three surfaces and reports the counts."""
    with (
        patch.object(
            inbound_recovery.email_inbox,
            "drain_unread_inboxes",
            return_value=4,
        ) as email,
        patch.object(
            inbound_recovery.sms_inbox,
            "replay_missed_messages",
            return_value=2,
        ) as sms,
        patch.object(
            inbound_recovery.telegram_inbox,
            "drain_pending_updates",
            return_value=1,
        ) as telegram,
    ):
        report = inbound_recovery.recover_missed_inbound()

    assert report.disabled is False
    assert report.emails_examined == 4
    assert report.messages_replayed == 2
    assert report.telegram_updates == 1
    assert report.errors == []
    email.assert_called_once()
    sms.assert_called_once()
    telegram.assert_called_once()
    assert "4 email" in report.summary()


def test_recover_keeps_going_when_one_channel_fails():
    """A Gmail failure must not skip SMS and Telegram."""
    with (
        patch.object(
            inbound_recovery.email_inbox,
            "drain_unread_inboxes",
            side_effect=RuntimeError("gmail down"),
        ),
        patch.object(
            inbound_recovery.sms_inbox,
            "replay_missed_messages",
            return_value=1,
        ),
        patch.object(
            inbound_recovery.telegram_inbox,
            "drain_pending_updates",
            return_value=0,
        ),
    ):
        report = inbound_recovery.recover_missed_inbound()

    assert report.emails_examined == 0
    assert report.messages_replayed == 1
    assert any(err.startswith("email:") for err in report.errors)


def test_email_drain_walks_pages_oldest_first(db):
    """A recovery pass keeps listing and handles each page oldest-first.

    Gmail returns newest-first. Flipping each page means two task
    emails that arrived while Avi was down are applied in send order.
    """
    pages = [["newer", "older"], ["oldest"]]
    handled: list[str] = []

    def _list(_creds, *, max_results=25):
        if not pages:
            return []
        return pages.pop(0)[:max_results]

    def _handle(_db, *, gmail_message_id, **_kwargs):
        handled.append(gmail_message_id)

    with (
        patch.object(
            email_inbox.google_oauth,
            "load_credentials",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch.object(
            email_inbox.gmail,
            "list_unread_inbox_message_ids",
            side_effect=_list,
        ),
        patch.object(email_inbox, "_already_processed", return_value=False),
        patch.object(email_inbox, "_handle_one_message", side_effect=_handle),
    ):
        examined = email_inbox.process_assistant_inbox(
            db,
            assistant_id=1,
            granted_email="avi@example.com",
            family_id=1,
            max_messages=10,
            drain=True,
        )

    assert handled == ["older", "newer", "oldest"]
    assert examined == ["newer", "older", "oldest"]


def test_replay_skips_sids_already_audited(db, monkeypatch):
    """A Twilio message we already processed is not sent through the agent again."""
    from api import models
    from api.config import get_settings
    from api.services import sms_inbox

    settings = get_settings()
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setattr(settings, "TWILIO_PRIMARY_PHONE", "+15550001111")
    monkeypatch.setattr(settings, "AI_SMS_INBOUND_ENABLED", True)
    monkeypatch.setattr(settings, "AI_WHATSAPP_INBOUND_ENABLED", False)

    seen_sid = f"SM_seen_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    seen = models.SmsInboxMessage(
        channel="sms",
        twilio_message_sid=seen_sid,
        from_phone="+15558675309",
        to_phone="+15550001111",
        body="old",
        num_media=0,
        status="processed_replied",
        received_at=datetime.now(timezone.utc),
    )
    db.add(seen)
    db.commit()

    fresh = twilio_sms.InboundSms(
        message_sid="SM_fresh",
        messaging_service_sid=None,
        account_sid="ACtest",
        from_phone="+15558675309",
        to_phone="+15550001111",
        body="add milk to the list",
        num_media=0,
        media=[],
        channel="sms",
    )
    already = twilio_sms.InboundSms(
        message_sid=seen_sid,
        messaging_service_sid=None,
        account_sid="ACtest",
        from_phone="+15558675309",
        to_phone="+15550001111",
        body="old",
        num_media=0,
        media=[],
        channel="sms",
    )
    sent = datetime.now(timezone.utc)
    with (
        patch.object(
            sms_inbox.twilio_sms,
            "list_inbound_messages",
            return_value=[(sent, already), (sent, fresh)],
        ),
        patch.object(sms_inbox, "process_inbound_sms") as process,
    ):
        replayed = sms_inbox.replay_missed_messages(lookback_hours=72, max_messages=10)

    assert replayed == 1
    process.assert_called_once()
    handed = process.call_args.args[1]
    assert handed.message_sid == "SM_fresh"
