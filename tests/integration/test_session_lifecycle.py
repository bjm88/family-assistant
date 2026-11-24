"""One session concept for every surface.

A session is one conversation: it ends after its surface's idle window
(live chat, SMS / WhatsApp / Telegram) — email sessions follow the Gmail
thread instead — and every surface feeds the agent the same transcript
slice via :func:`conversation_history`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from api import models
from api.ai import session as live_session
from api.config import get_settings


def _ago(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _thread_id() -> str:
    return f"+1555{uuid.uuid4().int % 10_000_000:07d}"


def test_text_thread_rolls_over_after_idle_window(db, test_family):
    fid = test_family["family_id"]
    phone = _thread_id()
    first, created = live_session.find_or_create_sms_session(
        db, family_id=fid, counterparty_phone=phone
    )
    db.commit()
    assert created

    again, created = live_session.find_or_create_sms_session(
        db, family_id=fid, counterparty_phone=phone
    )
    assert not created and again.live_session_id == first.live_session_id

    idle = get_settings().AI_THREAD_SESSION_IDLE_MINUTES
    first.last_activity_at = _ago(idle + 5)
    db.commit()
    fresh, created = live_session.find_or_create_sms_session(
        db, family_id=fid, counterparty_phone=phone
    )
    db.commit()
    assert created and fresh.live_session_id != first.live_session_id
    db.refresh(first)
    assert first.ended_at is not None and first.end_reason == "timeout"
    assert fresh.external_thread_id == phone and fresh.ended_at is None


def test_email_thread_never_idles_out(db, test_family):
    fid = test_family["family_id"]
    thread = f"thread-{uuid.uuid4().hex}"
    first, _ = live_session.find_or_create_email_session(
        db, family_id=fid, external_thread_id=thread, subject="Camp forms"
    )
    first.last_activity_at = _ago(60 * 24 * 30)
    db.commit()

    live_session.close_stale_sessions(db, family_id=fid)
    db.commit()
    db.refresh(first)
    assert first.ended_at is None

    again, created = live_session.find_or_create_email_session(
        db, family_id=fid, external_thread_id=thread
    )
    assert not created and again.live_session_id == first.live_session_id


def test_sweep_closes_idle_text_threads(db, test_family):
    fid = test_family["family_id"]
    s, _ = live_session.find_or_create_whatsapp_session(
        db, family_id=fid, counterparty_phone=_thread_id()
    )
    s.last_activity_at = _ago(get_settings().AI_THREAD_SESSION_IDLE_MINUTES + 1)
    db.commit()
    live_session.close_stale_sessions(db, family_id=fid)
    db.commit()
    db.refresh(s)
    assert s.end_reason == "timeout"


def test_conversation_history_is_the_transcript_without_acks(db, test_family):
    fid = test_family["family_id"]
    s, _ = live_session.find_or_create_telegram_session(
        db, family_id=fid, chat_id=uuid.uuid4().int % 10**9
    )
    turns = [
        ("assistant", "Welcome!", "telegram_invite_welcome"),
        ("user", "What's on Saturday?", "telegram"),
        ("assistant", "Looking that up.", "telegram_fast_ack"),
        ("assistant", "Soccer at 9.", "telegram_reply"),
        ("user", "And Sunday?", "telegram"),
        ("assistant", "Nothing yet.", "telegram_reply"),
    ]
    for role, content, kind in turns:
        live_session.log_message(db, s, role=role, content=content, meta={"kind": kind})
    db.commit()

    assert live_session.conversation_history(db, s) == [
        {"role": "user", "content": "What's on Saturday?"},
        {"role": "assistant", "content": "Soccer at 9."},
        {"role": "user", "content": "And Sunday?"},
        {"role": "assistant", "content": "Nothing yet."},
    ]
    assert live_session.conversation_history(db, s, max_messages=2) == [
        {"role": "user", "content": "And Sunday?"},
        {"role": "assistant", "content": "Nothing yet."},
    ]


def test_new_session_starts_with_empty_history(db, test_family):
    s, _ = live_session.find_or_create_sms_session(
        db, family_id=test_family["family_id"], counterparty_phone=_thread_id()
    )
    db.commit()
    assert live_session.conversation_history(db, s) == []
    assert isinstance(s, models.LiveSession)
