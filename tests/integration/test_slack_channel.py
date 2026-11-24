"""Slack as an inbound channel: webhook → shared agent pipeline → reply.

Slack is one more adapter in front of the same brain SMS, WhatsApp and
Telegram use. These tests post signed Events API payloads at the real
``/api/slack/events`` route and check what a family member would care
about:

* only people in the household get an answer, matched by the email on
  their Slack account;
* the answer goes back into the conversation the message came from, as
  the identity that received it (the Avi app, or Avi's own account);
* Avi never answers himself, an edit, a stranger, or the same message
  twice;
* shared files reach the agent the way MMS / WhatsApp media do;
* messages that arrived while the server was down are picked up.

Nothing here talks to slack.com: conftest blocks the HTTP layer and the
``workspace`` fixture stands in for the Slack Web API.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import select

from api import models
from api.ai import fast_action_router
from api.config import get_settings
from api.integrations import slack
from api.services import slack_inbox, system_status


AGENT_REPLY = "Mocked agent reply for the Slack suite."
SIGNING_SECRET = "test-slack-signing-secret"
BOT_TOKEN = "xoxb-test-bot-token"
USER_TOKEN = "xoxp-test-user-token"
APP_ID = "A0TESTAPP"

# 1x1 transparent PNG — enough bytes to store as a real file.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)

_ts_counter = itertools.count(1)


def _new_ts() -> str:
    """A unique Slack message timestamp ("1732468800.000123")."""
    return f"{int(time.time())}.{next(_ts_counter):06d}"


def _heavy_triage() -> fast_action_router.TriageResult:
    return fast_action_router.TriageResult(label=fast_action_router.LABEL_HEAVY)


# ---------------------------------------------------------------------------
# A stand-in Slack workspace
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    """What the fake Slack Web API knows, and what Avi did to it."""

    team_id: str
    bot_user_id: str
    human_user_id: str
    member_id: str  # the integration-test family member's Slack account
    users: dict[str, slack.SlackUser] = field(default_factory=dict)
    posts: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    history_calls: list[dict[str, Any]] = field(default_factory=list)
    file_bytes: Optional[bytes] = PNG_BYTES
    users_info_down: bool = False

    def dm_channel(self) -> str:
        return f"D{uuid.uuid4().hex[:10].upper()}"

    def add_user(self, user_id: str, email: Optional[str], **kwargs) -> str:
        self.users[user_id] = slack.SlackUser(
            user_id=user_id,
            team_id=kwargs.pop("team_id", self.team_id),
            email=email,
            display_name=kwargs.pop("display_name", "Someone"),
            **kwargs,
        )
        return user_id


@pytest.fixture
def workspace(monkeypatch, test_family) -> Workspace:
    """Fake Slack: two Avi identities, one family member, recorded sends.

    Each test gets its own team id so audit rows left behind by earlier
    runs never show up in another test's catch-up pass.
    """
    suffix = uuid.uuid4().hex[:8].upper()
    ws = Workspace(
        team_id=f"T{suffix}",
        bot_user_id=f"UBOT{suffix}",
        human_user_id=f"UAVI{suffix}",
        member_id=f"UMEM{suffix}",
    )
    ws.add_user(ws.member_id, test_family["person_email"], display_name="Integration Test")

    def fake_auth_test(token, **_kwargs):
        if token == BOT_TOKEN:
            return slack.SlackIdentity(
                team_id=ws.team_id,
                team_name="Test Household",
                user_id=ws.bot_user_id,
                user_name="avi_app",
                bot_id="B0TEST",
                url=None,
            )
        if token == USER_TOKEN:
            return slack.SlackIdentity(
                team_id=ws.team_id,
                team_name="Test Household",
                user_id=ws.human_user_id,
                user_name="avi.member",
                bot_id=None,
                url=None,
            )
        raise slack.SlackReadError("auth.test: invalid_auth", code="invalid_auth")

    def fake_get_user(_token, user_id):
        if ws.users_info_down:
            raise slack.SlackReadError("users.info: transport error: down")
        if user_id not in ws.users:
            raise slack.SlackReadError("users.info: user_not_found", code="user_not_found")
        return ws.users[user_id]

    def fake_post_message(**kwargs):
        ws.posts.append(kwargs)
        return _new_ts()

    def fake_download_file(**kwargs):
        ws.downloads.append(kwargs)
        if ws.file_bytes is None:
            raise slack.SlackReadError(
                "file download: Slack returned a sign-in page instead of the "
                "file — the token is missing the files:read scope.",
                code="missing_scope",
            )
        return slack.DownloadedFile(
            mime_type=kwargs.get("hint_mime") or "image/png", file_bytes=ws.file_bytes
        )

    def fake_history(**kwargs):
        ws.history_calls.append(kwargs)
        return list(ws.history.get(kwargs["channel"], []))

    monkeypatch.setattr(slack, "auth_test", fake_auth_test)
    monkeypatch.setattr(slack, "get_user", fake_get_user)
    monkeypatch.setattr(slack, "post_message", fake_post_message)
    monkeypatch.setattr(slack, "download_file", fake_download_file)
    monkeypatch.setattr(slack, "conversations_history", fake_history)
    return ws


def _message(
    ws: Workspace,
    *,
    text: str,
    channel: str,
    user: Optional[str] = None,
    ts: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """One Slack message object, as it appears in an event or in history."""
    return {
        "type": "message",
        "user": user or ws.member_id,
        "text": text,
        "ts": ts or _new_ts(),
        "channel": channel,
        **extra,
    }


def _payload(
    ws: Workspace, event: dict[str, Any], *, via: str = "bot", auth_user: Optional[str] = None
) -> dict[str, Any]:
    """Wrap a message in the Events API envelope Slack delivers."""
    if auth_user is None:
        auth_user = ws.bot_user_id if via == "bot" else ws.human_user_id
    return {
        "token": "deprecated-verification-token",
        "team_id": ws.team_id,
        "api_app_id": APP_ID,
        "type": "event_callback",
        "event_id": f"Ev{uuid.uuid4().hex[:12].upper()}",
        "event_time": int(time.time()),
        "authorizations": [
            {"team_id": ws.team_id, "user_id": auth_user, "is_bot": via == "bot"}
        ],
        "event": event,
    }


def _dm(ws: Workspace, text: str, *, via: str = "bot", **extra: Any) -> dict[str, Any]:
    """A direct message from the family member to one of Avi's identities."""
    channel = extra.pop("channel", None) or ws.dm_channel()
    return _payload(
        ws, _message(ws, text=text, channel=channel, channel_type="im", **extra), via=via
    )


def _post(client, payload: dict[str, Any], *, secret: str = SIGNING_SECRET, headers=None):
    """POST a payload to the webhook with a valid Slack signature."""
    body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    return client.post(
        "/api/slack/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": slack.sign(
                signing_secret=secret, timestamp=timestamp, body=body
            ),
            **(headers or {}),
        },
    )


def _deliver(client, payload: dict[str, Any], *, triage=None, agent_reply: Any = AGENT_REPLY):
    """Deliver an event with the brain mocked; return the agent mock."""
    agent_kwargs = (
        {"side_effect": agent_reply}
        if isinstance(agent_reply, Exception)
        else {"return_value": agent_reply}
    )
    with patch.object(
        slack_inbox, "_run_agent_to_completion", **agent_kwargs
    ) as agent_mock, patch.object(
        slack_inbox.fast_action_router,
        "try_triage_sync",
        return_value=triage or _heavy_triage(),
    ):
        resp = _post(client, payload)
    assert resp.status_code == 200, resp.text
    return agent_mock


def _audit(db, payload: dict[str, Any]) -> Optional[models.SlackInboxMessage]:
    db.expire_all()
    event = payload["event"]
    return db.execute(
        select(models.SlackInboxMessage)
        .where(models.SlackInboxMessage.slack_team_id == payload["team_id"])
        .where(models.SlackInboxMessage.slack_channel_id == event["channel"])
        .where(models.SlackInboxMessage.slack_message_ts == event["ts"])
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The webhook only listens to Slack
# ---------------------------------------------------------------------------


def test_webhook_rejects_a_forged_signature(client, workspace):
    payload = _dm(workspace, "Unlock the front door.")
    with patch.object(slack_inbox, "process_event") as process:
        resp = _post(client, payload, secret="someone-elses-secret")
    assert resp.status_code == 403
    process.assert_not_called()


def test_webhook_rejects_a_replayed_old_request(client, workspace):
    """A correctly signed request captured ten minutes ago is refused."""
    body = json.dumps(_dm(workspace, "hello")).encode("utf-8")
    stale = str(int(time.time()) - 600)
    with patch.object(slack_inbox, "process_event") as process:
        resp = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "x-slack-request-timestamp": stale,
                "x-slack-signature": slack.sign(
                    signing_secret=SIGNING_SECRET, timestamp=stale, body=body
                ),
            },
        )
    assert resp.status_code == 403
    process.assert_not_called()


def test_webhook_refuses_everything_without_a_signing_secret(client, workspace):
    """No secret configured means no request can be trusted — not "trust all"."""
    with patch.object(get_settings(), "SLACK_APP_SIGNING_SECRET", None), patch.object(
        slack_inbox, "process_event"
    ) as process:
        resp = _post(client, _dm(workspace, "hello"))
    assert resp.status_code == 503
    process.assert_not_called()


def test_webhook_answers_slacks_url_verification(client):
    resp = _post(client, {"type": "url_verification", "challenge": "abc123"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


# ---------------------------------------------------------------------------
# A family member talks to Avi
# ---------------------------------------------------------------------------


def test_dm_to_the_app_reaches_the_agent_and_is_answered(client, workspace, test_family, db):
    payload = _dm(workspace, "What's on the calendar tomorrow?")
    agent = _deliver(client, payload)

    audit = _audit(db, payload)
    assert audit.status == "processed_replied", audit.status_reason
    assert audit.person_id == test_family["person_id"]
    assert audit.family_id == test_family["family_id"]
    assert audit.via == "bot"
    assert audit.sender_email == test_family["person_email"]
    assert audit.reply_message_ts

    session = db.get(models.LiveSession, audit.live_session_id)
    assert session.source == "slack"
    task = db.get(models.AgentTask, audit.agent_task_id)
    assert task.kind == "slack"

    agent.assert_called_once()
    assert "What's on the calendar tomorrow?" in agent.call_args.kwargs["user_message"]
    assert "Slack" in agent.call_args.kwargs["system_prompt"]

    assert len(workspace.posts) == 1
    post = workspace.posts[0]
    assert post["token"] == BOT_TOKEN
    assert post["channel"] == payload["event"]["channel"]
    assert post["text"] == AGENT_REPLY
    assert post["thread_ts"] is None, "a DM is answered inline, not in a thread"


def test_dm_to_avis_own_account_is_answered_as_avi(client, workspace, db):
    """Messaging Avi like a person gets a reply from that same account."""
    payload = _dm(workspace, "Are you there?", via="user")
    _deliver(client, payload)

    assert _audit(db, payload).via == "user"
    assert [p["token"] for p in workspace.posts] == [USER_TOKEN]


def test_reply_inside_a_thread_stays_in_that_thread(client, workspace, db):
    parent = _new_ts()
    payload = _dm(workspace, "And the day after?", thread_ts=parent)
    _deliver(client, payload)

    assert workspace.posts[0]["thread_ts"] == parent
    assert _audit(db, payload).slack_thread_ts == parent


def test_conversation_continues_in_one_session(client, workspace, db):
    """Two messages in the same DM share a transcript, so Avi has context."""
    channel = workspace.dm_channel()
    first = _dm(workspace, "Remember the dentist is Tuesday.", channel=channel)
    second = _dm(workspace, "What did I just tell you?", channel=channel)
    _deliver(client, first)
    agent = _deliver(client, second)

    assert _audit(db, first).live_session_id == _audit(db, second).live_session_id
    history = agent.call_args.kwargs["history"]
    assert any("dentist is Tuesday" in (m.get("content") or "") for m in history)
    assert any(AGENT_REPLY in (m.get("content") or "") for m in history)


def test_sender_is_matched_by_email_ignoring_case(client, workspace, test_family, db):
    workspace.add_user(workspace.member_id, test_family["person_email"].upper())
    payload = _dm(workspace, "hello")
    _deliver(client, payload)
    assert _audit(db, payload).person_id == test_family["person_id"]


def test_sender_is_matched_by_a_work_email(client, workspace, test_family, db):
    """Someone who joined Slack with their work address is still recognised."""
    work_email = f"it.{uuid.uuid4().hex[:8]}@employer.example"
    job = models.Job(person_id=test_family["person_id"], work_email=work_email)
    db.add(job)
    db.commit()
    try:
        workspace.add_user(workspace.member_id, work_email)
        payload = _dm(workspace, "hello from work")
        _deliver(client, payload)
        assert _audit(db, payload).person_id == test_family["person_id"]
    finally:
        db.delete(job)
        db.commit()


def test_triage_shortcut_answers_without_the_heavy_agent(client, workspace, db):
    """Slack uses the same fast paths as the other channels."""
    fast_reply = "Added 'Call the plumber' to your tasks."
    payload = _dm(workspace, "Add a task to call the plumber.")
    agent = _deliver(
        client,
        payload,
        triage=fast_action_router.TriageResult(
            label=fast_action_router.LABEL_TASK_ADD,
            reply=fast_reply,
            used_path="fast_action",
            intent=fast_action_router.LABEL_TASK_ADD,
        ),
    )
    agent.assert_not_called()
    assert [p["text"] for p in workspace.posts] == [fast_reply]
    assert _audit(db, payload).status == "processed_replied"


def test_instant_ack_then_real_reply(client, workspace):
    payload = _dm(workspace, "Add a task to renew the passport.")
    with patch.object(get_settings(), "AI_FAST_ACK_ENABLED", True):
        _deliver(client, payload)
    assert [p["text"] for p in workspace.posts] == ["Setting that up.", AGENT_REPLY]


def test_agent_crash_still_gets_the_person_a_reply(client, workspace, db):
    payload = _dm(workspace, "What's for dinner?")
    _deliver(client, payload, agent_reply=RuntimeError("model fell over"))

    assert len(workspace.posts) == 1
    assert "snag" in workspace.posts[0]["text"]
    audit = _audit(db, payload)
    assert audit.status == "processed_replied"
    assert "crashed" in audit.status_reason


def test_reply_that_slack_rejects_is_recorded_as_failed(client, workspace, db, monkeypatch):
    def boom(**_kwargs):
        raise slack.SlackSendError("chat.postMessage: channel_not_found")

    monkeypatch.setattr(slack, "post_message", boom)
    payload = _dm(workspace, "hello")
    _deliver(client, payload)

    audit = _audit(db, payload)
    assert audit.status == "failed"
    assert "channel_not_found" in audit.status_reason


def test_markup_in_the_reply_cannot_ping_the_workspace(client, workspace):
    """The model's text is escaped, so "<!channel>" is just characters."""
    payload = _dm(workspace, "Summarise the week.")
    _deliver(client, payload, agent_reply="**Done** <!channel> see <@U123> & [notes](https://x.test/a)")

    sent = workspace.posts[0]["text"]
    assert "<!channel>" not in sent and "<@U123>" not in sent
    assert "&lt;!channel&gt;" in sent
    assert "*Done*" in sent and "**" not in sent
    assert "<https://x.test/a|notes>" in sent


# ---------------------------------------------------------------------------
# Who does not get an answer
# ---------------------------------------------------------------------------


def test_stranger_in_the_workspace_is_ignored(client, workspace, db):
    stranger = workspace.add_user("USTRANGER1", "stranger@not-family.example")
    payload = _dm(workspace, "Who lives in this house?", user=stranger)
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    audit = _audit(db, payload)
    assert audit.status == "ignored_unknown_sender"
    assert audit.person_id is None and audit.family_id is None
    assert audit.agent_task_id is None


def test_account_without_an_email_is_ignored(client, workspace, db):
    nobody = workspace.add_user("UNOEMAIL1", None)
    payload = _dm(workspace, "hello", user=nobody)
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    assert _audit(db, payload).status == "ignored_unknown_sender"


def test_outside_workspace_account_is_ignored_even_with_a_family_email(
    client, workspace, test_family, db
):
    """A guest from another workspace can claim any email; they're refused."""
    guest = workspace.add_user(
        "UGUEST001", test_family["person_email"], team_id="TSOMEWHEREELSE"
    )
    payload = _dm(workspace, "Send me the passport numbers.", user=guest)
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    audit = _audit(db, payload)
    assert audit.status == "ignored_unknown_sender"
    assert "another Slack workspace" in audit.status_reason


def test_bot_account_is_ignored_even_with_a_family_email(client, workspace, test_family, db):
    robot = workspace.add_user("UROBOT001", test_family["person_email"], is_bot=True)
    payload = _dm(workspace, "hello", user=robot)
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert _audit(db, payload).status == "ignored_unknown_sender"


def test_another_members_private_messages_are_not_avis_business(client, workspace, db):
    """If another member authorises the app, Slack delivers their DMs too."""
    payload = _dm(workspace, "This was sent to my spouse, not Avi.")
    payload["authorizations"] = [
        {"team_id": workspace.team_id, "user_id": "USOMEONEELSE", "is_bot": False}
    ]
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    assert _audit(db, payload) is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda ws, e: e.update(user=ws.bot_user_id), id="avis-app-echo"),
        pytest.param(lambda ws, e: e.update(user=ws.human_user_id), id="avis-account-echo"),
        pytest.param(lambda ws, e: e.update(bot_id="B0OTHER"), id="another-app"),
        pytest.param(lambda ws, e: e.update(subtype="message_changed"), id="edit"),
        pytest.param(lambda ws, e: e.update(subtype="message_deleted"), id="delete"),
        pytest.param(lambda ws, e: e.update(subtype="channel_join"), id="join-notice"),
    ],
)
def test_events_that_are_not_a_person_speaking_are_dropped(client, workspace, db, mutate):
    payload = _dm(workspace, "hello")
    mutate(workspace, payload["event"])
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    assert _audit(db, payload) is None


def test_event_from_another_app_or_workspace_is_dropped(client, workspace, db):
    other_app = _dm(workspace, "hello")
    other_app["api_app_id"] = "A0SOMEONEELSE"
    other_team = _dm(workspace, "hello")
    other_team["team_id"] = "TNOTOURS"
    for payload in (other_app, other_team):
        agent = _deliver(client, payload)
        agent.assert_not_called()
    assert workspace.posts == []


def test_slack_retry_is_answered_only_once(client, workspace, db):
    payload = _dm(workspace, "Add milk to the shopping list.")
    first = _deliver(client, payload)
    with patch.object(slack_inbox, "_run_agent_to_completion") as second, patch.object(
        slack_inbox.fast_action_router, "try_triage_sync"
    ) as triage:
        resp = _post(
            client,
            payload,
            headers={"x-slack-retry-num": "1", "x-slack-retry-reason": "http_timeout"},
        )

    assert resp.status_code == 200
    first.assert_called_once()
    second.assert_not_called()
    triage.assert_not_called()
    assert len(workspace.posts) == 1


def test_switched_off_records_the_message_but_does_not_answer(client, workspace, db):
    payload = _dm(workspace, "hello")
    with patch.object(get_settings(), "AI_SLACK_INBOUND_ENABLED", False):
        agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    audit = _audit(db, payload)
    assert audit.status == "failed"
    assert audit.status_reason == "AI_SLACK_INBOUND_ENABLED=false"


# ---------------------------------------------------------------------------
# Channels: only when Avi is addressed
# ---------------------------------------------------------------------------


def test_channel_chatter_that_doesnt_mention_avi_is_ignored(client, workspace, db):
    payload = _payload(
        workspace,
        _message(workspace, text="Anyone want pizza?", channel="C0FAMILY01", channel_type="channel"),
    )
    agent = _deliver(client, payload)

    agent.assert_not_called()
    assert workspace.posts == []
    assert _audit(db, payload) is None


def test_channel_mention_is_answered_in_a_thread(client, workspace, db):
    text = f"<@{workspace.bot_user_id}>, when is trash day?"
    event = _message(workspace, text=text, channel="C0FAMILY01")
    event["type"] = "app_mention"
    payload = _payload(workspace, event)
    agent = _deliver(client, payload)

    agent.assert_called_once()
    user_message = agent.call_args.kwargs["user_message"]
    assert "when is trash day?" in user_message
    assert workspace.bot_user_id not in user_message
    assert "Everyone in that channel can read" in agent.call_args.kwargs["system_prompt"]

    post = workspace.posts[0]
    assert post["channel"] == "C0FAMILY01"
    assert post["thread_ts"] == event["ts"], "channel replies go in a thread"
    assert _audit(db, payload).channel_type == "channel"


def test_mention_of_avis_own_account_in_a_private_channel_is_answered_as_avi(
    client, workspace, db
):
    """"@Avi Maisano ..." in a family channel, delivered for Avi's account."""
    text = f"<@{workspace.human_user_id}> what time is pickup?"
    event = _message(workspace, text=text, channel="G0PARENTS1", channel_type="group")
    payload = _payload(workspace, event, via="user")
    agent = _deliver(client, payload)

    agent.assert_called_once()
    assert "what time is pickup?" in agent.call_args.kwargs["user_message"]
    post = workspace.posts[0]
    assert post["token"] == USER_TOKEN
    assert post["thread_ts"] == event["ts"]
    audit = _audit(db, payload)
    assert audit.via == "user" and audit.channel_type == "group"


def test_channel_reply_comes_from_the_identity_that_was_mentioned(client, workspace, db):
    """Slack names one authorization per event; it may not be the one mentioned."""
    to_account = _payload(
        workspace,
        _message(
            workspace,
            text=f"<@{workspace.human_user_id}> are you there?",
            channel="G0PARENTS2",
            channel_type="group",
        ),
        via="bot",
    )
    to_app = _payload(
        workspace,
        _message(
            workspace,
            text=f"<@{workspace.bot_user_id}> are you there?",
            channel="G0PARENTS2",
            channel_type="group",
        ),
        via="user",
    )
    _deliver(client, to_account)
    _deliver(client, to_app)

    assert [p["token"] for p in workspace.posts] == [USER_TOKEN, BOT_TOKEN]
    assert _audit(db, to_account).via == "user"
    assert _audit(db, to_app).via == "bot"


def test_one_channel_message_delivered_as_two_events_is_answered_once(client, workspace):
    """Slack sends both ``app_mention`` and ``message`` for one mention."""
    ts = _new_ts()
    text = f"<@{workspace.bot_user_id}> what's the wifi password?"
    mention = _message(workspace, text=text, channel="C0FAMILY02", ts=ts)
    mention["type"] = "app_mention"
    plain = _message(workspace, text=text, channel="C0FAMILY02", ts=ts, channel_type="channel")

    first = _deliver(client, _payload(workspace, mention))
    second = _deliver(client, _payload(workspace, plain))

    first.assert_called_once()
    second.assert_not_called()
    assert len(workspace.posts) == 1


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _file(name: str = "insurance-card.png", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"F{uuid.uuid4().hex[:10].upper()}",
        "name": name,
        "mimetype": "image/png",
        "size": len(PNG_BYTES),
        "url_private_download": f"https://files.slack.com/files-pri/T0-F0/download/{name}",
        **extra,
    }


def test_shared_file_is_stored_and_handed_to_the_agent(client, workspace, db):
    payload = _dm(
        workspace, "Here's the new insurance card.", subtype="file_share", files=[_file()]
    )
    with patch.object(slack_inbox.vision, "describe_many", return_value=([], 0)), patch.object(
        slack_inbox.attachment_intake, "auto_file_attachments", return_value=""
    ) as intake:
        agent = _deliver(client, payload)

    audit = _audit(db, payload)
    assert audit.status == "processed_replied", audit.status_reason
    assert audit.num_media == 1
    assert workspace.downloads[0]["token"] == BOT_TOKEN

    stored = db.execute(
        select(models.SlackInboxAttachment).where(
            models.SlackInboxAttachment.slack_inbox_message_id
            == audit.slack_inbox_message_id
        )
    ).scalar_one()
    assert stored.media_index == 1
    assert stored.filename == "insurance-card.png"
    on_disk = get_settings().storage_root / stored.stored_path
    assert on_disk.read_bytes() == PNG_BYTES

    refs = agent.call_args.kwargs["inbound_attachments"]
    assert [(r.media_index, r.filename, r.channel) for r in refs] == [
        (1, "insurance-card.png", "slack")
    ]
    assert refs[0].stored_path == stored.stored_path
    intake.assert_called_once()
    assert "with 1 attached file" in agent.call_args.kwargs["user_message"]


def test_file_sent_to_avis_account_is_downloaded_with_that_accounts_token(client, workspace):
    payload = _dm(workspace, "see attached", via="user", subtype="file_share", files=[_file()])
    with patch.object(slack_inbox.vision, "describe_many", return_value=([], 0)), patch.object(
        slack_inbox.attachment_intake, "auto_file_attachments", return_value=""
    ):
        _deliver(client, payload)
    assert workspace.downloads[0]["token"] == USER_TOKEN


def test_message_with_a_file_skips_the_fast_paths(client, workspace):
    """The triage shortcuts have no attachment tools; files need the agent."""
    payload = _dm(workspace, "Add this to my documents.", subtype="file_share", files=[_file()])
    with patch.object(slack_inbox.vision, "describe_many", return_value=([], 0)), patch.object(
        slack_inbox.attachment_intake, "auto_file_attachments", return_value=""
    ), patch.object(slack_inbox, "_run_agent_to_completion", return_value=AGENT_REPLY), patch.object(
        slack_inbox.fast_action_router, "try_triage_sync", return_value=_heavy_triage()
    ) as triage:
        _post(client, payload)

    assert triage.call_args.kwargs["allow_action"] is False
    assert triage.call_args.kwargs["allow_family_qa"] is False
    assert triage.call_args.kwargs["surface_label"] == "Slack"


def test_file_avi_cannot_open_is_reported_not_silently_dropped(client, workspace, db):
    """Missing files:read must not look like "there was no attachment"."""
    workspace.file_bytes = None
    payload = _dm(workspace, "Can you file this?", subtype="file_share", files=[_file("lease.pdf")])
    agent = _deliver(client, payload)

    kwargs = agent.call_args.kwargs
    assert kwargs["inbound_attachments"] == []
    assert "could NOT open" in kwargs["user_message"]
    assert "lease.pdf" in kwargs["user_message"]
    audit = _audit(db, payload)
    assert audit.status == "processed_replied"
    assert "could not be retrieved" in audit.status_reason
    assert "files:read" in audit.status_reason


def test_oversized_file_is_not_downloaded(client, workspace):
    too_big = get_settings().AI_ATTACHMENT_MAX_BYTES + 1
    payload = _dm(
        workspace, "big one", subtype="file_share", files=[_file("movie.mov", size=too_big)]
    )
    agent = _deliver(client, payload)

    assert workspace.downloads == []
    assert "too large" in agent.call_args.kwargs["user_message"]


# ---------------------------------------------------------------------------
# Downtime catch-up
# ---------------------------------------------------------------------------


def _replay(**kwargs):
    with patch.object(
        slack_inbox, "_run_agent_to_completion", return_value=AGENT_REPLY
    ) as agent, patch.object(
        slack_inbox.fast_action_router, "try_triage_sync", return_value=_heavy_triage()
    ):
        count = slack_inbox.replay_missed_messages(
            **{"lookback_hours": 72, "max_messages": 10, **kwargs}
        )
    return count, agent


def test_catch_up_answers_only_what_was_missed(client, workspace, db):
    channel = workspace.dm_channel()
    seen = _dm(workspace, "First message, delivered normally.", channel=channel)
    _deliver(client, seen)
    workspace.posts.clear()

    missed_ts = _new_ts()
    workspace.history[channel] = [
        seen["event"],
        # Avi's own earlier reply is in the history too.
        _message(workspace, text=AGENT_REPLY, channel=channel, user=workspace.bot_user_id),
        _message(workspace, text="Sent while the server was down.", channel=channel, ts=missed_ts),
    ]

    count, agent = _replay()
    assert count == 1
    agent.assert_called_once()
    assert "Sent while the server was down." in agent.call_args.kwargs["user_message"]
    assert [p["channel"] for p in workspace.posts] == [channel]
    assert workspace.history_calls[0]["token"] == BOT_TOKEN

    # Running it again finds nothing new.
    count, agent = _replay()
    assert count == 0
    agent.assert_not_called()
    assert len(workspace.posts) == 1


def test_catch_up_never_reaches_back_before_avi_joined_the_conversation(client, workspace):
    """Turning Slack on must not make Avi answer old history."""
    channel = workspace.dm_channel()
    first = _dm(workspace, "hello", channel=channel)
    _deliver(client, first)

    _replay()
    assert workspace.history_calls[0]["oldest"] == first["event"]["ts"]


def test_catch_up_respects_the_message_cap(client, workspace):
    channel = workspace.dm_channel()
    _deliver(client, _dm(workspace, "hello", channel=channel))
    workspace.history[channel] = [
        _message(workspace, text=f"missed {n}", channel=channel) for n in range(5)
    ]
    count, agent = _replay(max_messages=2)
    assert count == 2
    assert agent.call_count == 2


def test_message_is_left_for_catch_up_when_slack_cant_identify_the_sender(
    client, workspace, db
):
    """If the sender lookup fails we must not record "unknown" and move on."""
    channel = workspace.dm_channel()
    _deliver(client, _dm(workspace, "an earlier message", channel=channel))
    workspace.posts.clear()

    workspace.users_info_down = True
    slack._USER_CACHE.clear()
    lost = _dm(workspace, "Sent during a Slack hiccup.", channel=channel)
    agent = _deliver(client, lost)
    agent.assert_not_called()
    assert _audit(db, lost) is None

    workspace.users_info_down = False
    workspace.history[channel] = [lost["event"]]
    count, agent = _replay()
    assert count == 1
    assert _audit(db, lost).status == "processed_replied"


def test_catch_up_does_nothing_when_slack_is_switched_off(workspace):
    with patch.object(get_settings(), "AI_SLACK_INBOUND_ENABLED", False):
        count, _agent = _replay()
    assert count == 0
    assert workspace.history_calls == []


# ---------------------------------------------------------------------------
# Around the edges
# ---------------------------------------------------------------------------


def test_slack_session_is_readable_through_the_sessions_api(client, workspace, db):
    payload = _dm(workspace, "hello")
    _deliver(client, payload)
    session_id = _audit(db, payload).live_session_id

    resp = client.get(f"/api/aiassistant/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "slack"
    contents = [m["content"] for m in body["messages"]]
    assert any("hello" in c for c in contents)
    assert AGENT_REPLY in contents


def test_status_page_reports_both_identities(workspace):
    check = asyncio.run(system_status.check_slack_api())
    assert check.status == "ok", check.summary
    assert "avi_app" in check.summary and "avi.member" in check.summary
    assert check.detail["events_url"].endswith("/api/slack/events")


def test_status_page_flags_a_revoked_token(workspace):
    with patch.object(get_settings(), "SLACK_USER_TOKEN", "xoxp-revoked"):
        check = asyncio.run(system_status.check_slack_api())
    assert check.status == "degraded"
    assert "invalid_auth" in check.summary


def test_status_page_flags_a_missing_signing_secret(workspace):
    with patch.object(get_settings(), "SLACK_APP_SIGNING_SECRET", None):
        check = asyncio.run(system_status.check_slack_api())
    assert check.status == "down"


# ---------------------------------------------------------------------------
# The adapter's pure functions
# ---------------------------------------------------------------------------


def test_signature_check_details():
    body = b'{"type":"event_callback"}'
    now = 1_800_000_000
    good = slack.sign(signing_secret="s3cret", timestamp=str(now), body=body)
    ok = dict(signing_secret="s3cret", timestamp=str(now), body=body, signature=good, now=now)

    assert slack.verify_signature(**ok)
    assert not slack.verify_signature(**{**ok, "body": body + b" "})
    assert not slack.verify_signature(**{**ok, "signing_secret": "other"})
    assert not slack.verify_signature(**{**ok, "signing_secret": None})
    assert not slack.verify_signature(**{**ok, "signature": None})
    assert not slack.verify_signature(**{**ok, "timestamp": "not-a-number"})
    assert not slack.verify_signature(**{**ok, "now": now + 301})
    assert not slack.verify_signature(**{**ok, "now": now - 301})


def test_inbound_text_is_unwrapped_from_slack_markup():
    names = {"U111": "Sarah"}
    text = (
        "<@UAVI>: tell <@U111> &amp; <@U222|Tom> about <https://ex.test/a|the plan> "
        "in <#C1|general> — email <mailto:a@b.test|a@b.test> if 2 &lt; 3 :tada:"
    )
    cleaned = slack.clean_text(text, strip_user_ids={"UAVI"}, resolve_user=names.get)
    assert cleaned == (
        "tell @Sarah & @Tom about the plan (https://ex.test/a) "
        "in #general — email a@b.test if 2 < 3 :tada:"
    )
    # Punctuation is only trimmed when a mention of Avi was removed.
    assert slack.clean_text(": not a mention") == ": not a mention"


def test_forwarded_message_text_is_included():
    parsed = slack.parse_message(
        {
            "type": "message",
            "user": "U1",
            "text": "Can you add this?",
            "ts": "1.000001",
            "channel": "D1",
            "attachments": [
                {"is_share": True, "author_name": "School Office", "text": "Picture day is Friday."}
            ],
        },
        team_id="T1",
        via=slack.VIA_BOT,
        bot_user_id="UBOT",
        human_user_id=None,
    )
    assert parsed.body == (
        "Can you add this?\n\n[Forwarded message from School Office]\nPicture day is Friday."
    )


def test_outbound_formatting_and_length():
    out = slack.format_outbound(
        "# Plan\n- **Milk** & eggs\n- call <Dr. Lee>\n__init__ and a_b_c stay put", max_chars=3000
    )
    assert out == (
        "*Plan*\n• *Milk* &amp; eggs\n• call &lt;Dr. Lee&gt;\n__init__ and a_b_c stay put"
    )
    long = slack.format_outbound("word " * 2000, max_chars=200)
    assert len(long) <= 200 and long.endswith("…")


def test_file_download_only_ever_sends_the_token_to_slack(monkeypatch):
    """A crafted file URL must not be able to collect the bearer token."""
    requested: list[str] = []
    monkeypatch.setattr(
        slack.httpx, "get", lambda url, **_kw: requested.append(url) or pytest.fail(url)
    )
    for url in (
        "https://evil.example/files-pri/x",
        "https://files.slack.com.evil.example/x",
        "http://files.slack.com/x",
    ):
        with pytest.raises(slack.SlackReadError, match="non-Slack URL"):
            slack.download_file(token="xoxb-anything", url=url)
    assert requested == []


def test_sign_in_page_instead_of_a_file_is_named_as_a_missing_scope(monkeypatch):
    """Without files:read Slack answers 200 with HTML, not an error."""

    class _Html:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<html>Sign in to Slack</html>"

    monkeypatch.setattr(slack.httpx, "get", lambda *_a, **_kw: _Html())
    with pytest.raises(slack.SlackReadError, match="files:read") as err:
        slack.download_file(
            token="xoxb-anything",
            url="https://files.slack.com/files-pri/T0-F0/download/card.png",
            hint_mime="image/png",
        )
    assert err.value.code == "missing_scope"


def test_web_api_error_names_the_missing_scope(monkeypatch):
    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        @staticmethod
        def json():
            return {"ok": False, "error": "missing_scope", "needed": "users:read.email"}

    monkeypatch.setattr(slack.httpx, "get", lambda *_a, **_kw: _Resp())
    with pytest.raises(slack.SlackReadError, match="users:read.email") as err:
        slack.get_user("xoxb-anything", "U1")
    assert err.value.code == "missing_scope"


def test_unfaked_slack_calls_fail_instead_of_reaching_the_network():
    """The safety net in conftest: no test can talk to slack.com."""
    with pytest.raises(slack.SlackReadError, match="transport error"):
        slack.auth_test("xoxb-anything")
    with pytest.raises(slack.SlackSendError, match="transport error"):
        slack.post_message(token="xoxb-anything", channel="D1", text="hi")
