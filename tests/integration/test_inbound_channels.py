"""Prove every inbound surface flows into the shared agent layer.

The point of this file is *not* to exercise individual surface details
(those have unit-style smoke tests during development). It's to lock in
the architectural invariant that **every channel routes user input
through the same agent brain and produces the expected per-surface
side effects**:

* SMS via the Twilio webhook.
* WhatsApp via the same Twilio webhook (channel auto-detected from the
  ``whatsapp:`` prefix).
* Telegram via the polling-loop's per-update entry point.
* Email via the polling-loop's per-message entry point.

For each, we mock out the LLM and the outbound network call (Twilio,
Telegram, Gmail) and assert:

1. The right audit row was written (channel / status).
2. A LiveSession was created/reused on the correct ``source``.
3. The agent wrapper (``_run_agent_to_completion``) was actually
   invoked — proving the inbound made it past every gate to the brain.
4. The outbound sender was called with the agent's reply text.

Mocking ``_run_agent_to_completion`` (the per-service synchronous
wrapper that drives ``ai.agent.run_agent``) keeps these tests fast and
deterministic: no Ollama, no Gemini, no real Twilio / Telegram /
Gmail, no fast-ack race threading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import desc, select

from api import models
from api.integrations import twilio_sms


AGENT_REPLY = "Mocked agent reply for the integration suite."


# ---------------------------------------------------------------------------
# SMS — POSTs to the actual Twilio webhook router
# ---------------------------------------------------------------------------


def _twilio_form(message_sid: str, *, from_phone: str, to_phone: str, body: str) -> dict[str, str]:
    """Minimal Twilio inbound form payload our parser will accept."""
    return {
        "MessageSid": message_sid,
        "AccountSid": "ACtest",
        "From": from_phone,
        "To": to_phone,
        "Body": body,
        "NumMedia": "0",
    }


def test_sms_inbound_routes_to_agent(client, test_family, db):
    """A real POST to /api/sms/twilio/inbound lands in the SMS pipeline."""
    sid = f"SM_test_sms_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    form = _twilio_form(
        sid,
        from_phone=test_family["person_phone"],
        to_phone="+15550001111",
        body="Hi Avi, integration test — please reply.",
    )

    sms_calls: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.sms_inbox.web_search_shortcut.try_shortcut_sync",
        return_value=None,
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=lambda **kw: sms_calls.append(kw) or "SMmocked-out-sid",
    ):
        resp = client.post("/api/sms/twilio/inbound", data=form)

    # Twilio expects a 2xx no matter what — surface failures are logged.
    assert resp.status_code in (200, 204), resp.text

    audit = db.execute(
        select(models.SmsInboxMessage)
        .where(models.SmsInboxMessage.twilio_message_sid == sid)
    ).scalar_one()
    assert audit.channel == "sms"
    assert audit.status == "processed_replied", (
        f"Expected processed_replied, got {audit.status!r} "
        f"(reason={audit.status_reason!r})"
    )
    assert audit.person_id == test_family["person_id"]
    assert audit.live_session_id is not None

    sess = db.get(models.LiveSession, audit.live_session_id)
    assert sess.source == "sms"

    agent_mock.assert_called_once()
    assert len(sms_calls) == 1
    assert sms_calls[0]["body"] == AGENT_REPLY
    assert sms_calls[0]["to_phone"] == test_family["person_phone"]


def test_whatsapp_inbound_routes_to_agent(client, test_family, db):
    """The WhatsApp ``whatsapp:`` prefix flips channel + session source."""
    sid = f"SM_test_wa_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    form = _twilio_form(
        sid,
        from_phone=f"whatsapp:{test_family['person_phone']}",
        to_phone="whatsapp:+15550002222",
        body="Hi Avi via WhatsApp, integration test.",
    )

    wa_calls: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.sms_inbox.web_search_shortcut.try_shortcut_sync",
        return_value=None,
    ), patch(
        "api.integrations.twilio_sms.send_whatsapp",
        side_effect=lambda **kw: wa_calls.append(kw) or "SMmocked-wa-sid",
    ):
        resp = client.post("/api/sms/twilio/inbound", data=form)

    assert resp.status_code in (200, 204), resp.text

    audit = db.execute(
        select(models.SmsInboxMessage)
        .where(models.SmsInboxMessage.twilio_message_sid == sid)
    ).scalar_one()
    assert audit.channel == "whatsapp"
    assert audit.status == "processed_replied", (
        f"Expected processed_replied, got {audit.status!r} "
        f"(reason={audit.status_reason!r})"
    )

    sess = db.get(models.LiveSession, audit.live_session_id)
    assert sess.source == "whatsapp", (
        "WhatsApp must produce a separate session source from SMS even "
        "for the same phone number — fix from migration 0028 must hold."
    )

    agent_mock.assert_called_once()
    assert len(wa_calls) == 1
    # The send helper auto-prefixes whatsapp: when given a bare phone;
    # we don't care about the exact prefix here, just that the body
    # routed through the WhatsApp sender (not send_sms).
    assert wa_calls[0]["body"] == AGENT_REPLY


# ---------------------------------------------------------------------------
# Telegram — direct service call (bypasses the polling loop)
# ---------------------------------------------------------------------------


def _telegram_update(*, update_id: int, chat_id: int, user_id: int, body: str) -> dict:
    """Smallest Bot API ``update`` shape ``parse_inbound_update`` accepts."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "IntegrationTest",
            },
            "text": body,
        },
    }


def test_telegram_inbound_routes_to_agent(test_family, db):
    """``process_inbound_update`` resolves a known telegram_user_id → agent."""
    from api.services import telegram_inbox

    # Make sure our test person is reachable by Telegram for this run.
    person = db.get(models.Person, test_family["person_id"])
    person.telegram_user_id = 99887766
    db.commit()

    update_id = int(datetime.now(timezone.utc).timestamp())
    update = _telegram_update(
        update_id=update_id,
        chat_id=99887766,
        user_id=99887766,
        body="Telegram integration test.",
    )

    sent: list[dict[str, Any]] = []

    with patch.object(
        telegram_inbox, "_run_agent_to_completion", return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.telegram_inbox.web_search_shortcut.try_shortcut_sync",
        return_value=None,
    ), patch(
        # send_message returns the int message_id of the sent reply, not
        # a message object. The audit + transcript meta record this id,
        # so it MUST be a real int — a MagicMock leaks into JSONB and
        # blows up at INSERT time.
        "api.integrations.telegram.send_message",
        side_effect=lambda **kw: sent.append(kw) or 424242,
    ):
        audit = telegram_inbox.process_inbound_update(
            db, update, bot_user_id=None
        )

    assert audit is not None
    assert audit.status == "processed_replied", (
        f"Expected processed_replied, got {audit.status!r} "
        f"(reason={audit.status_reason!r})"
    )
    assert audit.person_id == test_family["person_id"]

    sess = db.get(models.LiveSession, audit.live_session_id)
    assert sess.source == "telegram"

    agent_mock.assert_called_once()
    assert len(sent) == 1
    assert sent[0]["text"] == AGENT_REPLY


# ---------------------------------------------------------------------------
# Email — direct service call with a fake FetchedMessage
# ---------------------------------------------------------------------------


def test_email_inbound_routes_to_agent(test_family, db):
    """``_handle_one_message`` — fake Gmail msg → agent → reply send."""
    from api.integrations import gmail
    from api.services import email_inbox

    msg_id = f"msg_test_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    fake_msg = gmail.FetchedMessage(
        message_id=msg_id,
        thread_id=f"thr_{msg_id}",
        sender_email=test_family["person_email"],
        sender_name="Integration Test",
        subject="Integration test ping",
        body_text="Hi Avi, can you confirm you got this email?",
        in_reply_to_header=None,
        references_header=None,
        list_id_header=None,
        precedence_header=None,
        auto_submitted_header=None,
        received_at=datetime.now(timezone.utc),
        label_ids=["INBOX", "UNREAD"],
    )

    sent: list[dict[str, Any]] = []

    with patch(
        "api.services.email_inbox.gmail.fetch_message", return_value=fake_msg,
    ), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.email_inbox.web_search_shortcut.try_shortcut_sync",
        return_value=None,
    ), patch(
        # The email service calls ``gmail.send_reply`` (NOT send_email)
        # because every reply has to thread under the original message
        # via In-Reply-To / References headers. Patch the imported
        # module attribute the service actually uses.
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: sent.append(kw) or "gmail_sent_id_xyz",
    ):
        email_inbox._handle_one_message(
            db,
            assistant_id=test_family["assistant_id"],
            granted_email="avi-bot@example.com",
            family_id=test_family["family_id"],
            creds=MagicMock(),
            gmail_message_id=msg_id,
        )

    audit = db.execute(
        select(models.EmailInboxMessage)
        .where(models.EmailInboxMessage.gmail_message_id == msg_id)
    ).scalar_one()
    assert audit.status == "processed_replied", (
        f"Expected processed_replied, got {audit.status!r} "
        f"(reason={audit.status_reason!r})"
    )
    assert audit.person_id == test_family["person_id"]
    assert audit.live_session_id is not None

    sess = db.get(models.LiveSession, audit.live_session_id)
    assert sess.source == "email"

    agent_mock.assert_called_once()
    assert len(sent) == 1
    assert sent[0].get("body") == AGENT_REPLY


# ---------------------------------------------------------------------------
# Email subject MUST be considered by the LLM, not just the body
# ---------------------------------------------------------------------------


def _make_fake_email(*, subject: str | None, body: str | None):
    """Tiny FetchedMessage builder for the subject-vs-body tests."""
    from api.integrations import gmail

    return gmail.FetchedMessage(
        message_id="msg_subject_test",
        thread_id="thr_subject_test",
        sender_email="parent@example.com",
        sender_name="Test Parent",
        subject=subject,
        body_text=body,
        in_reply_to_header=None,
        references_header=None,
        list_id_header=None,
        precedence_header=None,
        auto_submitted_header=None,
        received_at=datetime.now(timezone.utc),
        label_ids=["INBOX", "UNREAD"],
    )


def test_email_combined_text_subject_only_is_returned():
    """Subject-only email: classifier must see the subject as the question."""
    from api.services import email_inbox

    text = email_inbox._combined_text_for_shortcut(
        _make_fake_email(
            subject="What's the latest Fed rate decision?", body=""
        )
    )
    assert text == "What's the latest Fed rate decision?"


def test_email_combined_text_body_only_is_returned():
    """Body-only email (no subject): classifier sees the body."""
    from api.services import email_inbox

    text = email_inbox._combined_text_for_shortcut(
        _make_fake_email(subject=None, body="What is the population of France?")
    )
    assert text == "What is the population of France?"


def test_email_combined_text_concatenates_subject_and_body():
    """Both present and distinct: classifier sees subject AND body."""
    from api.services import email_inbox

    text = email_inbox._combined_text_for_shortcut(
        _make_fake_email(
            subject="Quick question about Avi",
            body="Can you remind me when the truck registration expires?",
        )
    )
    assert "Quick question about Avi" in text
    assert "Can you remind me when the truck registration expires?" in text


def test_email_combined_text_strips_re_fwd_prefixes():
    """Thread-reply markers (Re:, Fwd:) are noise, not signal."""
    from api.services import email_inbox

    text = email_inbox._combined_text_for_shortcut(
        _make_fake_email(subject="Re: weather", body="What's the forecast?")
    )
    assert "Re:" not in text
    assert "weather" in text
    assert "What's the forecast?" in text


def test_email_combined_text_dedupes_when_subject_in_body():
    """If the subject is just a prefix of the body, don't double it."""
    from api.services import email_inbox

    body = "What is the speed limit on I-95?"
    text = email_inbox._combined_text_for_shortcut(
        _make_fake_email(subject="What is the speed limit", body=body)
    )
    assert text == body


def test_email_classifier_receives_subject_when_body_is_empty(
    test_family, db
):
    """End-to-end proof: subject-only email reaches the web-search shortcut.

    Regression test for the bug where the email handler passed only
    ``msg.body_text`` to ``web_search_shortcut.try_shortcut_sync`` —
    so a question typed entirely into the subject line never reached
    the classifier and was silently misrouted to the heavy agent.
    """
    from api.services import email_inbox

    msg_id = "msg_subjtest_" + str(
        int(datetime.now(timezone.utc).timestamp() * 1000000)
    )
    fake_msg = _make_fake_email(
        subject="What is the population of Tokyo right now?",
        body="",
    )
    fake_msg.message_id = msg_id
    fake_msg.thread_id = "thr_" + msg_id
    fake_msg.sender_email = test_family["person_email"]

    captured: list[str] = []

    def fake_shortcut(text: str):
        captured.append(text)
        return None

    with patch(
        "api.services.email_inbox.gmail.fetch_message", return_value=fake_msg,
    ), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ), patch(
        "api.services.email_inbox.web_search_shortcut.try_shortcut_sync",
        side_effect=fake_shortcut,
    ), patch(
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: "gmail_sent_id_xyz",
    ):
        email_inbox._handle_one_message(
            db,
            assistant_id=test_family["assistant_id"],
            granted_email="avi-bot@example.com",
            family_id=test_family["family_id"],
            creds=MagicMock(),
            gmail_message_id=msg_id,
        )

    assert len(captured) == 1, (
        f"Expected exactly one shortcut call, got {len(captured)}"
    )
    assert "Tokyo" in captured[0], (
        f"Subject keyword 'Tokyo' must reach the web-search classifier, "
        f"but it received: {captured[0]!r}"
    )


# ---------------------------------------------------------------------------
# Live web chat — sanity check the chat surface is mounted + healthy
# ---------------------------------------------------------------------------


def test_live_chat_status_endpoint_responds(client):
    """``GET /api/aiassistant/status`` is the live-web smoke probe.

    The full ``POST /chat`` SSE pipeline pulls in Ollama and is too
    heavy for the integration suite; the status endpoint exercises
    routing + the chat router module load path + the Ollama probe.
    It returns 200 with ``available=False`` when Ollama isn't running
    locally — that's still a healthy response shape, which is all we
    need to prove the live-web surface is mounted.
    """
    resp = client.get("/api/aiassistant/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body
    assert "model" in body
    assert "host" in body
