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
from api.ai import fast_action_router
from api.integrations import twilio_sms


AGENT_REPLY = "Mocked agent reply for the integration suite."


def _heavy_triage() -> fast_action_router.TriageResult:
    """Mock-friendly ``TriageResult`` that always routes to the heavy agent.

    Used wherever a test wants to verify that the unified triage
    fell through to the heavy agent — the surfaces all interpret
    ``label=HEAVY`` / ``reply=None`` as "run ``_run_agent_to_completion``".
    """
    return fast_action_router.TriageResult(
        label=fast_action_router.LABEL_HEAVY
    )


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
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
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


def test_sms_instant_ack_then_agent_reply(client, test_family, db):
    """SMS fires a keyword-bucket ack, then the real reply.

    The two-message contract: Avi confirms immediately (no LLM) and
    follows up after the agent (or triage) finishes. Tests force the
    ack flag on because conftest disables it to keep other mocks
    simple.
    """
    from api.config import get_settings

    sid = f"SM_test_sms_ack_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    form = _twilio_form(
        sid,
        from_phone=test_family["person_phone"],
        to_phone="+15550001111",
        body="Add a task to renew the passport.",
    )
    sms_calls: list[dict[str, Any]] = []

    with patch.object(get_settings(), "AI_FAST_ACK_ENABLED", True), patch(
        "api.services.sms_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=lambda **kw: sms_calls.append(kw) or f"SM{len(sms_calls)}",
    ):
        resp = client.post("/api/sms/twilio/inbound", data=form)

    assert resp.status_code in (200, 204), resp.text
    agent_mock.assert_called_once()
    assert len(sms_calls) == 2, (
        f"Expected instant ack + final reply, got {len(sms_calls)} sends"
    )
    assert sms_calls[0]["body"] == "Setting that up."
    assert sms_calls[1]["body"] == AGENT_REPLY


def test_sms_fast_action_skips_heavy_agent(client, test_family, db):
    """Unified triage reply is sent as-is; heavy agent is not invoked."""
    sid = f"SM_test_sms_fast_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    form = _twilio_form(
        sid,
        from_phone=test_family["person_phone"],
        to_phone="+15550001111",
        body="Add a task to renew the passport.",
    )
    sms_calls: list[dict[str, Any]] = []
    fast_reply = "Added 'Renew the passport' to your tasks."

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=fast_action_router.TriageResult(
            label=fast_action_router.LABEL_TASK_ADD,
            reply=fast_reply,
            used_path="fast_action",
            intent=fast_action_router.LABEL_TASK_ADD,
        ),
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=lambda **kw: sms_calls.append(kw) or "SMfast",
    ):
        resp = client.post("/api/sms/twilio/inbound", data=form)

    assert resp.status_code in (200, 204), resp.text
    agent_mock.assert_not_called()
    assert len(sms_calls) == 1
    assert sms_calls[0]["body"] == fast_reply


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
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
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


def test_whatsapp_instant_ack_then_agent_reply(client, test_family, db):
    """WhatsApp uses the same two-message instant-ack contract as SMS."""
    from api.config import get_settings

    sid = f"SM_test_wa_ack_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    form = _twilio_form(
        sid,
        from_phone=f"whatsapp:{test_family['person_phone']}",
        to_phone="whatsapp:+15550002222",
        body="Add a calendar hold next Monday at 10am.",
    )
    wa_calls: list[dict[str, Any]] = []

    with patch.object(get_settings(), "AI_FAST_ACK_ENABLED", True), patch(
        "api.services.sms_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
    ), patch(
        "api.integrations.twilio_sms.send_whatsapp",
        side_effect=lambda **kw: wa_calls.append(kw) or f"SM{len(wa_calls)}",
    ):
        resp = client.post("/api/sms/twilio/inbound", data=form)

    assert resp.status_code in (200, 204), resp.text
    agent_mock.assert_called_once()
    assert len(wa_calls) == 2
    assert wa_calls[0]["body"] == "Setting that up."
    assert wa_calls[1]["body"] == AGENT_REPLY


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
        "api.services.telegram_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
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
        "api.services.email_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
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


def test_email_fast_action_skips_heavy_agent(test_family, db):
    """Email triage reply is sent as-is; heavy agent is not invoked."""
    from api.integrations import gmail
    from api.services import email_inbox

    msg_id = f"msg_fast_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    fake_msg = gmail.FetchedMessage(
        message_id=msg_id,
        thread_id=f"thr_{msg_id}",
        sender_email=test_family["person_email"],
        sender_name="Integration Test",
        subject="Add a task",
        body_text="Add a task to call the plumber.",
        in_reply_to_header=None,
        references_header=None,
        list_id_header=None,
        precedence_header=None,
        auto_submitted_header=None,
        received_at=datetime.now(timezone.utc),
        label_ids=["INBOX", "UNREAD"],
    )
    sent: list[dict[str, Any]] = []
    fast_reply = "Added 'Call the plumber' to your tasks."

    with patch(
        "api.services.email_inbox.gmail.fetch_message", return_value=fake_msg,
    ), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ) as agent_mock, patch(
        "api.services.email_inbox.fast_action_router.try_triage_sync",
        return_value=fast_action_router.TriageResult(
            label=fast_action_router.LABEL_TASK_ADD,
            reply=fast_reply,
            used_path="fast_action",
            intent=fast_action_router.LABEL_TASK_ADD,
        ),
    ), patch(
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: sent.append(kw) or "gmail_sent_fast",
    ):
        email_inbox._handle_one_message(
            db,
            assistant_id=test_family["assistant_id"],
            granted_email="avi-bot@example.com",
            family_id=test_family["family_id"],
            creds=MagicMock(),
            gmail_message_id=msg_id,
        )

    agent_mock.assert_not_called()
    assert len(sent) == 1
    assert sent[0].get("body") == fast_reply


def test_family_qa_uses_fast_model_when_no_finetune():
    """Family-recall shortcut must not require a custom Ollama tag."""
    from api.ai import family_qa_router
    from api.config import get_settings

    settings = get_settings()
    with patch.object(settings, "AI_FAMILY_QA_MODEL", ""):
        tag = family_qa_router._answer_model()
    assert tag, "fast-model fallback must resolve to a non-empty tag"
    assert tag.startswith("grok"), (
        f"live provider is Grok; leftover local tags must not win, got {tag!r}"
    )


def test_family_qa_ignores_leftover_ollama_tag_when_provider_is_grok():
    """A stale AI_FAMILY_QA_MODEL=llama3.2:3b must not be sent to xAI."""
    from api.ai import family_qa_router
    from api.config import get_settings

    settings = get_settings()
    with patch.object(settings, "AI_FAMILY_QA_MODEL", "llama3.2:3b"):
        tag = family_qa_router._answer_model()
    assert tag.startswith("grok")


def test_email_never_sends_instant_ack(test_family, db):
    """Email stays one-reply even when the global ack flag is on."""
    from api.config import get_settings
    from api.integrations import gmail
    from api.services import email_inbox

    msg_id = f"msg_noack_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    fake_msg = gmail.FetchedMessage(
        message_id=msg_id,
        thread_id=f"thr_{msg_id}",
        sender_email=test_family["person_email"],
        sender_name="Integration Test",
        subject="Add a task",
        body_text="Add a task to buy milk.",
        in_reply_to_header=None,
        references_header=None,
        list_id_header=None,
        precedence_header=None,
        auto_submitted_header=None,
        received_at=datetime.now(timezone.utc),
        label_ids=["INBOX", "UNREAD"],
    )
    sent: list[dict[str, Any]] = []

    with patch.object(get_settings(), "AI_FAST_ACK_ENABLED", True), patch(
        "api.services.email_inbox.gmail.fetch_message", return_value=fake_msg,
    ), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ), patch(
        "api.services.email_inbox.fast_action_router.try_triage_sync",
        return_value=_heavy_triage(),
    ), patch(
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: sent.append(kw) or "gmail_sent_noack",
    ):
        email_inbox._handle_one_message(
            db,
            assistant_id=test_family["assistant_id"],
            granted_email="avi-bot@example.com",
            family_id=test_family["family_id"],
            creds=MagicMock(),
            gmail_message_id=msg_id,
        )

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
    """End-to-end proof: subject-only email reaches the triage classifier.

    Regression test for the bug where the email handler passed only
    ``msg.body_text`` to the shortcut — so a question typed entirely
    into the subject line never reached the classifier and was
    silently misrouted to the heavy agent. The handler now calls
    :func:`fast_action_router.try_triage_sync`; we capture its first
    positional arg to prove the subject made it in.
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

    def fake_triage(text: str, **_kwargs):
        captured.append(text)
        return _heavy_triage()

    with patch(
        "api.services.email_inbox.gmail.fetch_message", return_value=fake_msg,
    ), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        return_value=AGENT_REPLY,
    ), patch(
        "api.services.email_inbox.fast_action_router.try_triage_sync",
        side_effect=fake_triage,
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
        f"Expected exactly one triage call, got {len(captured)}"
    )
    assert "Tokyo" in captured[0], (
        f"Subject keyword 'Tokyo' must reach the triage classifier, "
        f"but it received: {captured[0]!r}"
    )


# ---------------------------------------------------------------------------
# Live web chat — sanity check the chat surface is mounted + healthy
# ---------------------------------------------------------------------------


def test_live_chat_status_endpoint_responds(authed_client):
    """``GET /api/aiassistant/status`` is the live-web smoke probe.

    The full ``POST /chat`` SSE pipeline pulls in Ollama and is too
    heavy for the integration suite; the status endpoint exercises
    routing + the chat router module load path + the Ollama probe.
    It returns 200 with ``available=False`` when Ollama isn't running
    locally — that's still a healthy response shape, which is all we
    need to prove the live-web surface is mounted.
    """
    resp = authed_client.get("/api/aiassistant/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body
    assert "model" in body
    assert "host" in body
