"""Avi sending a stored photo or document back with a reply.

"Send me a picture of Jackson's passport." Three things have to hold:

1. **The right file, to the right person.** The ``send_file`` tool
   finds the file that was asked for, and only approves it when the
   person asking may have it — an ID scan follows the same rule as the
   ID number (self, spouse, parent); photos and household documents go
   to any family member. When there is no picture, or it can't travel
   on this channel, the tool says so instead of approving.
2. **It actually arrives.** Each channel carries the approved file its
   own way: attached to the email, uploaded to Slack, sent as a
   Telegram photo/document, or — for WhatsApp and text messages —
   fetched by Twilio from a short-lived signed link.
3. **The link is safe to be public.** It serves one file, only while
   it is fresh, and only if it was signed by this server.

No test reaches Slack, Twilio, Gmail or Telegram: the senders are
replaced with recorders, and the agent is either scripted or replaced
by a stand-in that records the tool result the real one would.
"""

from __future__ import annotations

import asyncio
import base64
import email
import io
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from api import models
from api.ai import fast_action_router
from api.ai.tools import ToolContext, ToolError
from api.ai.tools.handlers import files as file_tools
from api.config import get_settings
from api.integrations import gmail, telegram, twilio_sms
from api.services import outbound_files

PUBLIC_URL = "https://avi.example.com"
REPLY = "Here's the passport."


@pytest.fixture
def public_url(monkeypatch):
    """Give the server a public https address, as it has in production."""
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", PUBLIC_URL)
    return PUBLIC_URL


def _ctx(db, hf, *, channel="slack", speaker: Any = "parent", **kwargs) -> ToolContext:
    return ToolContext(
        db=db,
        family_id=hf.family_id,
        person_id=hf.parent_id if speaker == "parent" else speaker,
        reply_channel=channel,
        **kwargs,
    )


def _send(ctx, **kwargs) -> dict:
    return asyncio.run(file_tools.handle_send_file(ctx, **kwargs))


def _passport_ref(hf) -> dict:
    return {"source": "identity_document", "record_id": hf.passport_id, "side": "front"}


def _heavy() -> fast_action_router.TriageResult:
    return fast_action_router.TriageResult(label=fast_action_router.LABEL_HEAVY)


# ---------------------------------------------------------------------------
# 1. The tool: which file, and who may have it
# ---------------------------------------------------------------------------


def test_parent_is_sent_their_childs_passport(db, household_files):
    hf = household_files
    ctx = _ctx(db, hf)

    out = _send(ctx, what="identity_document", person=hf.child_name, document_type="passport")

    assert out["attachments"] == [_passport_ref(hf)]
    assert out["attached"] == [f"{hf.child_name}'s passport (front)"]
    assert "Slack" in out["delivery"]
    # Nothing about where the file lives on disk reaches the model.
    assert "outbound_test" not in str(out)


def test_document_type_is_understood_however_it_is_phrased(db, household_files):
    hf = household_files
    for phrase in ("Passport", "passport"):
        out = _send(_ctx(db, hf), what="identity_document", person=hf.child_name, document_type=phrase)
        assert out["attachments"] == [_passport_ref(hf)]
    # "driver's license" finds the license record — which has no picture.
    for phrase in ("driver's license", "drivers license", "license", "Driver’s Licence"):
        with pytest.raises(ToolError, match="no picture of the front"):
            _send(_ctx(db, hf), what="identity_document", person=hf.child_name, document_type=phrase)


def test_missing_picture_is_reported_not_approved(db, household_files):
    hf = household_files
    ctx = _ctx(db, hf)

    with pytest.raises(ToolError) as err:
        _send(ctx, what="identity_document", person=hf.child_name, document_type="drivers_license")

    assert "driver's license is on record, but no picture" in str(err.value)
    assert "do not say a file is attached" in str(err.value)
    assert ctx.reply_files == []


def test_both_sides_sends_the_front_when_no_back_was_saved(db, household_files):
    hf = household_files
    out = _send(
        _ctx(db, hf), what="identity_document", person=hf.child_name,
        document_type="passport", side="both",
    )
    assert out["attachments"] == [_passport_ref(hf)]


def test_someone_elses_id_is_refused(db, household_files):
    """The integration person is not the uncle, his spouse, or his parent."""
    hf = household_files
    ctx = _ctx(db, hf)

    with pytest.raises(ToolError, match="stays private"):
        _send(ctx, what="identity_document", person=hf.uncle_name, document_type="passport")
    with pytest.raises(ToolError, match="stays private"):
        _send(ctx, what="identity_document", record_id=hf.uncle_passport_id)
    assert ctx.reply_files == []


def test_child_cannot_get_a_parents_or_an_uncles_id(db, household_files):
    hf = household_files
    ctx = _ctx(db, hf, speaker=hf.child_id)
    # Their own passport is fine.
    assert _send(ctx, what="identity_document", document_type="passport")["attachments"] == [
        _passport_ref(hf)
    ]
    with pytest.raises(ToolError, match="stays private"):
        _send(ctx, what="identity_document", record_id=hf.uncle_passport_id)


def test_unidentified_speaker_gets_nothing(db, household_files):
    hf = household_files
    with pytest.raises(ToolError, match="without knowing who is asking"):
        _send(_ctx(db, hf, speaker=None), what="vehicle_photo", record_id=hf.car_id)


def test_another_households_records_do_not_exist_here(db, household_files):
    hf = household_files
    with pytest.raises(ToolError, match="no ID document with that record_id"):
        _send(_ctx(db, hf), what="identity_document", record_id=hf.stranger_passport_id)
    # And someone from the other household can't ask this one for files.
    with pytest.raises(ToolError, match="members of this household"):
        _send(_ctx(db, hf, speaker=hf.stranger_id), what="vehicle_photo", record_id=hf.car_id)
    with pytest.raises(outbound_files.FileUnavailable):
        outbound_files.resolve(
            db, family_id=hf.family_id, source="identity_document",
            record_id=hf.stranger_passport_id, side="front",
        )


def test_car_photo_by_driver_or_by_make_goes_to_any_family_member(db, household_files):
    hf = household_files
    ref = {"source": "vehicle_photo", "record_id": hf.car_id, "side": None}

    by_driver = _send(_ctx(db, hf), what="vehicle_photo", person=hf.child_name)
    by_make = _send(_ctx(db, hf), what="vehicle_photo", query=f"the {hf.car_make} car")
    # The uncle is no closer to the child than anyone else; a car photo isn't private.
    for_uncle = _send(_ctx(db, hf, speaker=hf.uncle_id), what="vehicle_photo", query=hf.car_make)

    assert by_driver["attachments"] == by_make["attachments"] == for_uncle["attachments"] == [ref]
    assert by_driver["attached"] == [f"Photo of the 2021 {hf.car_make} 430"]


def test_unknown_car_lists_what_is_on_file(db, household_files):
    hf = household_files
    with pytest.raises(ToolError) as err:
        _send(_ctx(db, hf), what="vehicle_photo", query="Lamborghini zzqx")
    assert "No vehicle matches" in str(err.value)
    assert f"record_id {hf.car_id}" in str(err.value)


def test_household_document_goes_to_family_but_a_persons_document_is_private(
    db, household_files
):
    hf = household_files
    out = _send(_ctx(db, hf, channel="email"), what="document", query=f"dental insurance {hf.tag}")
    assert out["attachments"] == [{"source": "document", "record_id": hf.dental_id, "side": None}]

    # The uncle's tax return: by words or by id, not for the integration person…
    with pytest.raises(ToolError, match="stays private"):
        _send(_ctx(db, hf, channel="email"), what="document", query=f"tax return {hf.tag}")
    with pytest.raises(ToolError, match="stays private"):
        _send(_ctx(db, hf, channel="email"), what="document", record_id=hf.uncle_doc_id)
    # …but for the uncle himself.
    mine = _send(
        _ctx(db, hf, channel="email", speaker=hf.uncle_id), what="document",
        query=f"tax return {hf.tag}",
    )
    assert mine["attachments"][0]["record_id"] == hf.uncle_doc_id


def test_private_file_is_not_posted_where_others_can_read(db, household_files):
    hf = household_files
    shared = _ctx(db, hf, reply_is_shared=True)

    with pytest.raises(ToolError, match="Other people can read this conversation"):
        _send(shared, what="identity_document", person=hf.child_name, document_type="passport")
    # A photo of the car is fine in a shared channel.
    assert _send(shared, what="vehicle_photo", record_id=hf.car_id)["attachments"]


@pytest.mark.parametrize("channel", ["chat", "monitoring", None])
def test_no_files_where_there_is_no_reply_channel_to_carry_them(db, household_files, channel):
    hf = household_files
    with pytest.raises(ToolError, match="can't be sent in this conversation"):
        _send(_ctx(db, hf, channel=channel), what="vehicle_photo", record_id=hf.car_id)


def test_pdf_cannot_go_by_text_message_but_can_by_whatsapp(db, household_files, public_url):
    hf = household_files
    with pytest.raises(ToolError) as err:
        _send(_ctx(db, hf, channel="sms"), what="document", record_id=hf.dental_id)
    assert "A PDF can't be sent in a text message" in str(err.value)
    assert "Email or Slack" in str(err.value)

    assert _send(_ctx(db, hf, channel="whatsapp"), what="document", record_id=hf.dental_id)[
        "attachments"
    ]


def test_text_and_whatsapp_need_a_public_address(db, household_files, monkeypatch):
    hf = household_files
    monkeypatch.setattr(get_settings(), "PUBLIC_BASE_URL", "http://localhost:8000")
    for channel in ("sms", "whatsapp"):
        with pytest.raises(ToolError, match="isn't set up on this server"):
            _send(_ctx(db, hf, channel=channel), what="vehicle_photo", record_id=hf.car_id)
    # Channels that are handed the file directly don't care.
    assert _send(_ctx(db, hf, channel="email"), what="vehicle_photo", record_id=hf.car_id)


def test_asking_twice_for_the_same_file_sends_it_once(db, household_files):
    hf = household_files
    ctx = _ctx(db, hf)
    for _ in range(2):
        _send(ctx, what="vehicle_photo", record_id=hf.car_id)
    assert len(ctx.reply_files) == 1


# ---------------------------------------------------------------------------
# Fitting a file to a channel
# ---------------------------------------------------------------------------


def _passport(db, hf) -> outbound_files.OutboundFile:
    return outbound_files.resolve(
        db, family_id=hf.family_id, source="identity_document",
        record_id=hf.passport_id, side="front",
    )


def test_large_photo_is_shrunk_for_a_text_but_not_for_email(db, household_files):
    hf = household_files
    file = _passport(db, hf)
    assert file.size_bytes > 2 * 1024 * 1024, "fixture photo should be phone-sized"

    emailed = outbound_files.prepare(file, "email")
    texted = outbound_files.prepare(file, "sms")

    assert emailed.data == hf.passport_bytes  # the original, untouched
    assert texted.mime_type == "image/jpeg"
    assert len(texted.data) <= 800 * 1024
    with Image.open(io.BytesIO(texted.data)) as img:
        # Still big enough to read a passport.
        assert max(img.size) >= 1024
    # The stored original is never rewritten.
    assert outbound_files.prepare(file, "email").data == hf.passport_bytes


def test_small_picture_is_sent_as_it_is(db, household_files):
    hf = household_files
    car = outbound_files.resolve(
        db, family_id=hf.family_id, source="vehicle_photo", record_id=hf.car_id
    )
    for channel in ("sms", "whatsapp", "slack", "telegram", "email"):
        prepared = outbound_files.prepare(car, channel)
        assert prepared.data == hf.png_bytes
        assert prepared.mime_type == "image/png"


# ---------------------------------------------------------------------------
# 3. The signed link Twilio fetches
# ---------------------------------------------------------------------------


def _path(url: str) -> str:
    assert url.startswith(PUBLIC_URL + "/api/outbound-files/")
    return url[len(PUBLIC_URL):]


def test_signed_link_serves_the_file_without_a_login(client, db, household_files, public_url):
    hf = household_files
    url = outbound_files.signed_url(_passport(db, hf), family_id=hf.family_id, channel="sms")
    anonymous = TestClient(client.app)  # no session cookie, like Twilio

    resp = anonymous.get(_path(url))

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "no-store" in resp.headers["cache-control"]
    assert len(resp.content) <= 800 * 1024  # already shrunk for a text
    assert url.endswith(".jpg")


def test_signed_link_cannot_be_altered_forged_or_reused_late(
    client, db, household_files, public_url
):
    hf = household_files
    anonymous = TestClient(client.app)
    file = _passport(db, hf)
    token = outbound_files.sign_token(file, family_id=hf.family_id, channel="sms")
    payload, signature = token.split(".")

    def get(tok: str) -> int:
        return anonymous.get(f"/api/outbound-files/{tok}/x.jpg").status_code

    assert get(token) == 200
    # Point the same signature at a different record.
    other = outbound_files.sign_token(
        outbound_files.resolve(
            db, family_id=hf.family_id, source="identity_document",
            record_id=hf.uncle_passport_id, side="front",
        ),
        family_id=hf.family_id,
        channel="sms",
    )
    assert get(f"{other.split('.')[0]}.{signature}") == 404
    # No signature, a made-up one, and nonsense.
    assert get(payload) == 404
    assert get(f"{payload}.{'A' * len(signature)}") == 404
    assert get("not-a-token") == 404
    # A link past its time.
    expired = outbound_files.sign_token(
        file, family_id=hf.family_id, channel="sms", ttl_seconds=-5
    )
    assert get(expired) == 404


def test_signed_link_dies_with_the_record(client, db, household_files, public_url):
    hf = household_files
    token = outbound_files.sign_token(_passport(db, hf), family_id=hf.family_id, channel="sms")
    doc = db.get(models.IdentityDocument, hf.passport_id)
    doc.front_image_path = None
    db.commit()

    assert TestClient(client.app).get(f"/api/outbound-files/{token}/x.jpg").status_code == 404


def test_link_expiry_follows_the_setting(db, household_files, monkeypatch):
    hf = household_files
    monkeypatch.setattr(get_settings(), "AI_FILE_LINK_TTL_SECONDS", 120)
    token = outbound_files.sign_token(_passport(db, hf), family_id=hf.family_id, channel="sms")
    payload = token.split(".")[0]
    claims = __import__("json").loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert 100 <= claims["e"] - time.time() <= 121


# ---------------------------------------------------------------------------
# What the agent approved is what gets delivered
# ---------------------------------------------------------------------------


def _task(db, hf, kind: str) -> models.AgentTask:
    task = models.AgentTask(
        family_id=hf.family_id, person_id=hf.parent_id, kind=kind,
        status="pending", input_text="send me the passport",
    )
    db.add(task)
    db.commit()
    return task


def test_only_successful_send_file_results_are_delivered(
    db, household_files, attach_files_to_task
):
    hf = household_files
    task = _task(db, hf, "slack")
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            # A refused call leaves no output behind.
            models.AgentStep(
                agent_task_id=task.agent_task_id, step_index=0, step_type="tool_result",
                tool_name="send_file", tool_output=None, error="stays private", created_at=now,
            ),
            # Another tool's output is not a file to send, whatever it says.
            models.AgentStep(
                agent_task_id=task.agent_task_id, step_index=2, step_type="tool_result",
                tool_name="sql_query",
                tool_output={"attachments": [{"source": "identity_document",
                                              "record_id": hf.uncle_passport_id, "side": "front"}]},
                created_at=now,
            ),
        ]
    )
    db.commit()
    attach_files_to_task(
        task.agent_task_id,
        _passport_ref(hf),
        _passport_ref(hf),  # asked twice
        {"source": "identity_document", "record_id": hf.license_id, "side": "front"},
    )

    found = outbound_files.collect_for_task(
        db, task_id=task.agent_task_id, family_id=hf.family_id, channel="slack"
    )

    assert [f.ref() for f in found.files] == [_passport_ref(hf)]
    # The license has no picture: reported, not silently dropped.
    assert len(found.problems) == 1 and "no picture" in found.problems[0]


def test_scripted_agent_run_attaches_the_file_end_to_end(db, household_files):
    """The real agent loop: tool call → recorded result → file for the reply."""
    from api.ai import agent as agent_loop
    from api.ai import agent_drain
    from api.ai import tools as agent_tools
    from api.ai.llm_types import ChatWithToolsResult, ToolCall

    hf = household_files
    task = _task(db, hf, "email")
    turns = [
        ChatWithToolsResult(
            content="",
            tool_calls=[
                ToolCall(
                    name="send_file",
                    arguments={
                        "what": "identity_document",
                        "person": hf.child_name,
                        "document_type": "passport",
                    },
                    id="call_1",
                )
            ],
            model="scripted",
        ),
        ChatWithToolsResult(content=REPLY, model="scripted"),
    ]
    seen_tool_results: list[str] = []

    async def scripted(messages, **_kwargs):
        seen_tool_results.extend(
            str(m.get("content")) for m in messages if m.get("role") == "tool"
        )
        return turns.pop(0)

    with patch.object(agent_loop.llm, "chat_with_tools", scripted):
        result = agent_drain.drain_agent_sync(
            task_id=task.agent_task_id,
            family_id=hf.family_id,
            assistant_id=None,
            person_id=hf.parent_id,
            system_prompt="test",
            history=[],
            user_message=f"send me a picture of {hf.child_name}'s passport",
            registry=agent_tools.build_default_registry(),
            capabilities=set(),
        )

    assert result.final_text == REPLY, result.error_text
    # The model was told it worked, on the channel the task came in on.
    assert any("Approved" in r and "email" in r for r in seen_tool_results)
    found = outbound_files.collect_for_task(
        db, task_id=task.agent_task_id, family_id=hf.family_id, channel="email"
    )
    assert [f.ref() for f in found.files] == [_passport_ref(hf)]


# ---------------------------------------------------------------------------
# 2. Delivery on each channel
# ---------------------------------------------------------------------------


def _twilio_form(sid: str, *, from_phone: str, to_phone: str, body: str) -> dict[str, str]:
    return {
        "MessageSid": sid, "AccountSid": "ACtest", "From": from_phone,
        "To": to_phone, "Body": body, "NumMedia": "0",
    }


def _sid(prefix: str) -> str:
    return f"SM_{prefix}_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"


def _agent_attaching(attach, *refs):
    """A stand-in agent that 'calls' send_file for ``refs`` and replies."""

    def run(**kwargs):
        attach(kwargs["task_id"], *refs)
        return REPLY

    return run


def _reply_meta(db, live_session_id: int) -> dict:
    row = db.execute(
        select(models.LiveSessionMessage)
        .where(models.LiveSessionMessage.live_session_id == live_session_id)
        .where(models.LiveSessionMessage.role == "assistant")
        .order_by(models.LiveSessionMessage.live_session_message_id.desc())
        .limit(1)
    ).scalar_one()
    return row.meta or {}


def test_text_message_reply_carries_a_link_twilio_can_fetch(
    client, db, test_family, household_files, attach_files_to_task, public_url
):
    hf = household_files
    sid = _sid("file_sms")
    form = _twilio_form(
        sid, from_phone=test_family["person_phone"], to_phone="+15550001111",
        body="send me a picture of the passport",
    )
    sent: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        side_effect=_agent_attaching(attach_files_to_task, _passport_ref(hf)),
    ), patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=lambda **kw: sent.append(kw) or "SMfile-out",
    ):
        client.post("/api/sms/twilio/inbound", data=form)

    assert len(sent) == 1
    assert sent[0]["body"] == REPLY
    (url,) = sent[0]["media_urls"]
    fetched = TestClient(client.app).get(_path(url))
    assert fetched.status_code == 200 and fetched.headers["content-type"] == "image/jpeg"
    assert len(fetched.content) <= 800 * 1024

    audit = db.execute(
        select(models.SmsInboxMessage).where(models.SmsInboxMessage.twilio_message_sid == sid)
    ).scalar_one()
    assert audit.status == "processed_replied"
    assert f"{hf.child_name}'s passport (front)" in audit.status_reason
    meta = _reply_meta(db, audit.live_session_id)
    assert [a["record_id"] for a in meta["attachments_sent"]] == [hf.passport_id]
    # The transcript records what was sent, never where it is stored.
    assert "stored_path" not in str(meta) and "outbound_test" not in str(meta)


def test_whatsapp_sends_one_file_per_message(
    client, db, test_family, household_files, attach_files_to_task, public_url
):
    hf = household_files
    car_ref = {"source": "vehicle_photo", "record_id": hf.car_id, "side": None}
    form = _twilio_form(
        _sid("file_wa"), from_phone=f"whatsapp:{test_family['person_phone']}",
        to_phone="whatsapp:+15550002222", body="passport and the car please",
    )
    sent: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        side_effect=_agent_attaching(attach_files_to_task, _passport_ref(hf), car_ref),
    ), patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.integrations.twilio_sms.send_whatsapp",
        side_effect=lambda **kw: sent.append(kw) or f"SMwa-{len(sent)}",
    ):
        client.post("/api/sms/twilio/inbound", data=form)

    assert [len(m["media_urls"]) for m in sent] == [1, 1]
    assert sent[0]["body"] == REPLY
    assert sent[1]["body"] == f"Photo of the 2021 {hf.car_make} 430"
    assert sent[1]["media_urls"][0].endswith(".png")


def test_whatsapp_sends_the_words_first_when_the_file_is_not_a_picture(
    client, db, test_family, household_files, attach_files_to_task, public_url
):
    """WhatsApp shows a caption with a picture, not with a PDF."""
    hf = household_files
    form = _twilio_form(
        _sid("file_wa_pdf"), from_phone=f"whatsapp:{test_family['person_phone']}",
        to_phone="whatsapp:+15550002222", body="send the dental card",
    )
    sent: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        side_effect=_agent_attaching(
            attach_files_to_task, {"source": "document", "record_id": hf.dental_id, "side": None}
        ),
    ), patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.integrations.twilio_sms.send_whatsapp",
        side_effect=lambda **kw: sent.append(kw) or f"SMwa-{len(sent)}",
    ):
        client.post("/api/sms/twilio/inbound", data=form)

    assert sent[0]["body"] == REPLY and "media_urls" not in sent[0]
    assert len(sent[1]["media_urls"]) == 1 and sent[1]["media_urls"][0].endswith(".pdf")


def test_picture_that_cannot_be_converted_for_a_text_is_refused_up_front(
    db, household_files, public_url
):
    """A text needs a JPEG; a file that only claims to be an image can't become one."""
    from api import storage

    hf = household_files
    doc = db.get(models.IdentityDocument, hf.passport_id)
    storage.absolute_path(doc.front_image_path).write_bytes(b"not really a jpeg" * 100_000)

    with pytest.raises(ToolError, match="couldn't be converted"):
        _send(_ctx(db, hf, channel="sms"), what="identity_document", record_id=hf.passport_id)
    # Email takes whatever is stored.
    assert _send(_ctx(db, hf, channel="email"), what="identity_document", record_id=hf.passport_id)


def test_text_that_twilio_rejects_is_emailed_with_the_file_attached(
    client, db, test_family, household_files, attach_files_to_task, public_url
):
    hf = household_files
    form = _twilio_form(
        _sid("file_sms_fail"), from_phone=test_family["person_phone"],
        to_phone="+15550001111", body="send me a picture of the passport",
    )
    emails: list[dict[str, Any]] = []

    with patch(
        "api.services.sms_inbox._run_agent_to_completion",
        side_effect=_agent_attaching(attach_files_to_task, _passport_ref(hf)),
    ), patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=twilio_sms.TwilioSendError("HTTP 400: unregistered"),
    ), patch(
        "api.services.sms_inbox.google_oauth.load_credentials",
        return_value=(MagicMock(), MagicMock()),
    ), patch(
        "api.services.sms_inbox.gmail.send_email",
        side_effect=lambda creds, **kw: emails.append(kw) or "gmail-fallback",
    ):
        client.post("/api/sms/twilio/inbound", data=form)

    assert len(emails) == 1 and emails[0]["to"] == test_family["person_email"]
    (attachment,) = emails[0]["attachments"]
    # Email isn't size-limited like a text, so it gets the original.
    assert attachment.data == hf.passport_bytes


def test_undelivered_whatsapp_file_goes_with_the_sms_resend(
    client, db, test_family, household_files, attach_files_to_task, public_url, monkeypatch
):
    hf = household_files
    inbound_sid, reply_sid = _sid("file_wa_dlv"), _sid("file_wa_reply")
    person = db.get(models.Person, test_family["person_id"])
    before = (person.sms_opted_out, person.sms_opted_in_at)
    person.sms_opted_out, person.sms_opted_in_at = False, datetime.now(timezone.utc)
    db.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "TWILIO_PRIMARY_PHONE", "+15550001111")
    monkeypatch.setattr(settings, "AI_WHATSAPP_SMS_FALLBACK_ENABLED", True)
    sms: list[dict[str, Any]] = []
    try:
        with patch(
            "api.services.sms_inbox._run_agent_to_completion",
            side_effect=_agent_attaching(attach_files_to_task, _passport_ref(hf)),
        ), patch(
            "api.services.sms_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
        ), patch("api.integrations.twilio_sms.send_whatsapp", return_value=reply_sid):
            client.post(
                "/api/sms/twilio/inbound",
                data=_twilio_form(
                    inbound_sid, from_phone=f"whatsapp:{test_family['person_phone']}",
                    to_phone="whatsapp:+15550002222", body="passport please",
                ),
            )
        with patch(
            "api.integrations.twilio_sms.send_sms",
            side_effect=lambda **kw: sms.append(kw) or "SMresend",
        ):
            client.post(
                "/api/sms/twilio/status",
                data={
                    "MessageSid": reply_sid, "MessageStatus": "failed", "ErrorCode": "63112",
                    "From": "whatsapp:+15550002222",
                    "To": f"whatsapp:{test_family['person_phone']}",
                },
            )
    finally:
        person.sms_opted_out, person.sms_opted_in_at = before
        db.commit()

    assert len(sms) == 1 and REPLY in sms[0]["body"]
    (url,) = sms[0]["media_urls"]
    assert TestClient(client.app).get(_path(url)).status_code == 200


def test_email_reply_has_the_file_attached(
    db, test_family, household_files, attach_files_to_task
):
    from api.services import email_inbox

    hf = household_files
    dental_ref = {"source": "document", "record_id": hf.dental_id, "side": None}
    msg_id = f"msg_file_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    fake_msg = gmail.FetchedMessage(
        message_id=msg_id, thread_id=f"thr_{msg_id}",
        sender_email=test_family["person_email"], sender_name="Integration Test",
        subject="Passport", body_text="Can you send me the passport and the dental card?",
        in_reply_to_header=None, references_header=None, list_id_header=None,
        precedence_header=None, auto_submitted_header=None,
        received_at=datetime.now(timezone.utc), label_ids=["INBOX", "UNREAD"],
    )
    sent: list[dict[str, Any]] = []

    with patch("api.services.email_inbox.gmail.fetch_message", return_value=fake_msg), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch(
        "api.services.email_inbox._run_agent_to_completion",
        side_effect=_agent_attaching(attach_files_to_task, _passport_ref(hf), dental_ref),
    ), patch(
        "api.services.email_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: sent.append(kw) or "gmail_sent",
    ):
        email_inbox._handle_one_message(
            db, assistant_id=test_family["assistant_id"], granted_email="avi-bot@example.com",
            family_id=test_family["family_id"], creds=MagicMock(), gmail_message_id=msg_id,
        )

    assert len(sent) == 1 and sent[0]["to"] == test_family["person_email"]
    passport, dental = sent[0]["attachments"]
    assert passport.data == hf.passport_bytes and passport.mime_type == "image/jpeg"
    assert passport.filename.endswith("passport-front.jpg")
    assert dental.data == hf.pdf_bytes and dental.mime_type == "application/pdf"
    audit = db.execute(
        select(models.EmailInboxMessage).where(models.EmailInboxMessage.gmail_message_id == msg_id)
    ).scalar_one()
    assert audit.status == "processed_replied"
    assert "Sent file(s)" in audit.status_reason


def test_email_says_so_when_an_approved_file_has_gone_missing(
    db, test_family, household_files, attach_files_to_task
):
    """Approved by the tool, then the picture is deleted before the reply goes."""
    from api.services import email_inbox

    hf = household_files
    msg_id = f"msg_file_gone_{int(datetime.now(timezone.utc).timestamp() * 1e6)}"
    fake_msg = gmail.FetchedMessage(
        message_id=msg_id, thread_id=f"thr_{msg_id}",
        sender_email=test_family["person_email"], sender_name="Integration Test",
        subject="Passport", body_text="Send me the passport",
        in_reply_to_header=None, references_header=None, list_id_header=None,
        precedence_header=None, auto_submitted_header=None,
        received_at=datetime.now(timezone.utc), label_ids=["INBOX", "UNREAD"],
    )
    sent: list[dict[str, Any]] = []

    def agent(**kwargs):
        attach_files_to_task(kwargs["task_id"], _passport_ref(hf))
        doc = db.get(models.IdentityDocument, hf.passport_id)
        doc.front_image_path = None
        db.commit()
        return REPLY

    with patch("api.services.email_inbox.gmail.fetch_message", return_value=fake_msg), patch(
        "api.services.email_inbox._safe_mark_read", return_value=None,
    ), patch("api.services.email_inbox._run_agent_to_completion", side_effect=agent), patch(
        "api.services.email_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.services.email_inbox.gmail.send_reply",
        side_effect=lambda *a, **kw: sent.append(kw) or "gmail_sent",
    ):
        email_inbox._handle_one_message(
            db, assistant_id=test_family["assistant_id"], granted_email="avi-bot@example.com",
            family_id=test_family["family_id"], creds=MagicMock(), gmail_message_id=msg_id,
        )

    assert "attachments" not in sent[0]
    assert sent[0]["body"].startswith(REPLY)
    assert "no picture of the front has been saved" in sent[0]["body"]


def test_telegram_reply_is_followed_by_the_file(
    db, test_family, household_files, attach_files_to_task, monkeypatch
):
    from api.services import telegram_inbox

    hf = household_files
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "test-bot-token")
    person = db.get(models.Person, test_family["person_id"])
    person.telegram_user_id = 99887766
    db.commit()
    update_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": int(time.time()),
            "chat": {"id": 99887766, "type": "private"},
            "from": {"id": 99887766, "is_bot": False, "first_name": "IntegrationTest"},
            "text": "send me the passport",
        },
    }
    texts: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    agent_kwargs: list[dict[str, Any]] = []

    def agent(**kwargs):
        agent_kwargs.append(kwargs)
        return _agent_attaching(attach_files_to_task, _passport_ref(hf))(**kwargs)

    with patch.object(telegram_inbox, "_run_agent_to_completion", side_effect=agent), patch(
        "api.services.telegram_inbox.fast_action_router.try_triage_sync", return_value=_heavy(),
    ), patch(
        "api.integrations.telegram.send_message",
        side_effect=lambda **kw: texts.append(kw) or 424242,
    ), patch(
        "api.integrations.telegram.send_file",
        side_effect=lambda **kw: files.append(kw) or 424243,
    ):
        audit = telegram_inbox.process_inbound_update(db, update, bot_user_id=None)

    assert audit.status == "processed_replied"
    assert [t["text"] for t in texts] == [REPLY]
    (file,) = files
    assert file["chat_id"] == 99887766
    assert file["data"] == hf.passport_bytes
    assert file["caption"] == f"{hf.child_name}'s passport (front)"
    # A private chat: the agent is told nobody else is reading.
    assert agent_kwargs[0]["shared_conversation"] is False


def test_shortcut_replies_never_carry_files(
    client, db, test_family, household_files, attach_files_to_task, public_url
):
    """Only a reply the heavy agent wrote can have files attached to it."""
    form = _twilio_form(
        _sid("file_shortcut"), from_phone=test_family["person_phone"],
        to_phone="+15550001111", body="what's the weather",
    )
    sent: list[dict[str, Any]] = []
    with patch("api.services.sms_inbox._run_agent_to_completion") as agent, patch(
        "api.services.sms_inbox.fast_action_router.try_triage_sync",
        return_value=fast_action_router.TriageResult(
            label=fast_action_router.LABEL_WEB, reply="Sunny.", used_path="web_search"
        ),
    ), patch(
        "api.integrations.twilio_sms.send_sms",
        side_effect=lambda **kw: sent.append(kw) or "SMshortcut",
    ):
        client.post("/api/sms/twilio/inbound", data=form)

    agent.assert_not_called()
    assert len(sent) == 1 and "media_urls" not in sent[0]


# ---------------------------------------------------------------------------
# The channel senders themselves
# ---------------------------------------------------------------------------


def test_gmail_builds_a_real_attachment(monkeypatch):
    sent: dict[str, Any] = {}

    class FakeSend:
        def __init__(self, body):
            sent.update(body)

        def execute(self):
            return {"id": "gmail-1"}

    service = MagicMock()
    service.users.return_value.messages.return_value.send.side_effect = (
        lambda userId, body: FakeSend(body)
    )
    monkeypatch.setattr(gmail, "_service", lambda creds: service)
    pdf = outbound_files.PreparedFile(
        filename="dental-insurance.pdf", mime_type="application/pdf", data=b"%PDF-1.4 test"
    )

    gmail.send_reply(
        MagicMock(), to="member@example.com", subject="Card", body="Attached.",
        in_reply_to="<a@b>", references=None, thread_id="thr1", attachments=[pdf],
    )

    message = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
    assert message.is_multipart()
    parts = {p.get_filename(): p for p in message.walk() if p.get_filename()}
    assert parts["dental-insurance.pdf"].get_content_type() == "application/pdf"
    assert parts["dental-insurance.pdf"].get_payload(decode=True) == b"%PDF-1.4 test"
    text = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    assert "Attached." in text.get_payload(decode=True).decode()
    assert sent["threadId"] == "thr1"


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_telegram_falls_back_to_a_document_when_the_photo_is_refused(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        method = url.rsplit("/", 1)[-1]
        calls.append((method, kwargs))
        if method == "sendPhoto":
            return _Resp(400, {"ok": False, "description": "PHOTO_INVALID_DIMENSIONS"})
        return _Resp(200, {"ok": True, "result": {"message_id": 77}})

    monkeypatch.setattr(telegram.httpx, "post", fake_post)

    message_id = telegram.send_file(
        bot_token="t", chat_id=5, filename="scan.jpg", data=b"jpegbytes",
        mime_type="image/jpeg", caption="Passport",
    )

    assert message_id == 77
    assert [m for m, _ in calls] == ["sendPhoto", "sendDocument"]
    assert calls[1][1]["files"]["document"] == ("scan.jpg", b"jpegbytes", "image/jpeg")
    assert calls[1][1]["data"] == {"chat_id": "5", "caption": "Passport"}

    calls.clear()
    telegram.send_file(
        bot_token="t", chat_id=5, filename="card.pdf", data=b"%PDF",
        mime_type="application/pdf",
    )
    assert [m for m, _ in calls] == ["sendDocument"]


# ---------------------------------------------------------------------------
# Getting the request to the agent that has the tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "send me a picture of Jackson's passport",
        "Can you text me a photo of the BMW?",
        "email me the dental insurance pdf",
        "show me Jax's license photo",
        "I need a copy of my passport",
        "share the insurance card with me",
        "do you have a scan of Paisley's birth certificate",
        "Send me an image of their car",
    ],
)
def test_file_requests_go_to_the_agent_that_can_send_files(message):
    assert fast_action_router.is_file_request(message)
    assert asyncio.run(fast_action_router.classify_triage(message)) == (
        fast_action_router.LABEL_HEAVY
    )


@pytest.mark.parametrize(
    "message",
    [
        "what's on my calendar tomorrow?",
        "when does Jackson's passport expire?",
        "send Mom a note thanking her for dinner",
        "add milk to the list",
        "what's the weather in Asheville",
    ],
)
def test_ordinary_questions_are_not_mistaken_for_file_requests(message):
    assert not fast_action_router.is_file_request(message)
