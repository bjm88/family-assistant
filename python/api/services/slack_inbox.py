"""Slack-driven AI assistant — Events API webhook + per-message agent dispatch.

Big picture
-----------
Slack is one more way for **registered family members** to talk to Avi.
Slack pushes each message to ``POST /api/slack/events``
(``routers/slack_webhook.py``); that handler verifies the signature and
hands the payload to :func:`process_event` on a worker thread. From
there the pipeline is the same one SMS, WhatsApp and Telegram run:
dedup → who is this → session + transcript → instant ack → triage
shortcut → heavy agent → reply → audit row.

Who Avi is on Slack
-------------------
Avi can be reached two ways in the workspace, and both work:

* **The app** (``SLACK_BOT_TOKEN``) — a member DMs the Avi app, or
  mentions it in a channel it has been added to.
* **Avi's own member account** (``SLACK_USER_TOKEN``) — a member DMs
  Avi exactly like they would DM a person.

Every inbound records which identity received it (``via``) and the
reply, any file download and any catch-up read use that identity's
token, so the answer always appears in the conversation it came from.

Who gets an answer
------------------
The sender's Slack account email (verified by Slack) is matched against
``people.email_address`` — or the ``work_email`` of one of their jobs —
the same rule the email inbox uses. **No match, no reply**: the message
is recorded as ``ignored_unknown_sender`` and the agent never runs.
Accounts from another workspace and bot accounts are refused before the
email is even looked at.

Where the reply goes
--------------------
* A direct message is answered inline, like any chat.
* A message inside a thread is answered in that thread.
* In a channel Avi only answers when mentioned, and replies in a thread
  under the mention so the channel stays readable.

What this code deliberately does not do
---------------------------------------
* Answer its own messages. Avi's replies come back from Slack as events;
  they are dropped before anything else happens.
* React to edits, deletions, join notices or other apps' posts.
* Act on events Slack delivers for another member's authorisation (an
  app can be authorised by several people; their private messages are
  not addressed to Avi).
* Start a conversation. This module only replies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Mapping, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai import agent as agent_loop
from ..ai import agent_drain
from ..ai import assistants as _assistants
from ..ai import fast_ack
from ..ai import fast_action_router
from ..ai import llm
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..ai import vision
from ..config import Settings, get_settings
from ..db import SessionLocal
from ..integrations import slack
from . import attachment_intake, inbound_prompts, outbound_files


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Avi's Slack identities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Avi:
    """Avi's own Slack user ids — needed to spot his messages echoing back."""

    team_id: str
    bot_user_id: Optional[str]
    human_user_id: Optional[str]


def _avi(settings: Settings) -> _Avi:
    """Resolve Avi's identities from the configured tokens.

    A token that can't be checked right now is treated as absent, which
    drops events for that identity rather than risk Avi answering
    himself. Raises :class:`slack.SlackReadError` when neither works.
    """
    identities: dict[str, slack.SlackIdentity] = {}
    problems: List[str] = []
    for via, token in (
        (slack.VIA_BOT, settings.SLACK_BOT_TOKEN),
        (slack.VIA_USER, settings.SLACK_USER_TOKEN),
    ):
        if not token:
            continue
        try:
            identities[via] = slack.auth_test_cached(token)
        except slack.SlackReadError as exc:
            problems.append(f"{via} token: {exc}")
    if not identities:
        raise slack.SlackReadError(
            "; ".join(problems) or "No SLACK_BOT_TOKEN or SLACK_USER_TOKEN configured."
        )
    if problems:
        logger.warning("Slack inbox: %s", "; ".join(problems))
    bot = identities.get(slack.VIA_BOT)
    human = identities.get(slack.VIA_USER)
    if bot and human and bot.team_id != human.team_id:
        raise slack.SlackReadError(
            "SLACK_BOT_TOKEN and SLACK_USER_TOKEN belong to different workspaces."
        )
    return _Avi(
        team_id=(bot or human).team_id,
        bot_user_id=bot.user_id if bot else None,
        human_user_id=human.user_id if human else None,
    )


def _token_for(via: str, settings: Settings) -> Optional[str]:
    """The token of the identity that received a message."""
    if via == slack.VIA_BOT:
        return settings.SLACK_BOT_TOKEN
    return settings.SLACK_USER_TOKEN


def _lookup_user(settings: Settings, user_id: str) -> slack.SlackUser:
    """``users.info`` with whichever token is allowed to read it.

    The bot token normally carries ``users:read.email``; the member
    token is the fallback when the bot isn't configured.
    """
    last: Optional[slack.SlackReadError] = None
    for token in (settings.SLACK_BOT_TOKEN, settings.SLACK_USER_TOKEN):
        if not token:
            continue
        try:
            return slack.get_user_cached(token, user_id)
        except slack.SlackReadError as exc:
            last = exc
    raise last or slack.SlackReadError("No Slack token configured.")


def _name_resolver(settings: Settings) -> Callable[[str], Optional[str]]:
    """Best-effort ``U123 -> "Sarah"`` for mentions inside a message."""

    def _resolve(user_id: str) -> Optional[str]:
        try:
            return _lookup_user(settings, user_id).display_name
        except slack.SlackReadError:
            return None

    return _resolve


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def process_event(
    db: Session, payload: Mapping
) -> Optional[models.SlackInboxMessage]:
    """Handle one verified Events API payload.

    Returns the audit row, or ``None`` when the event wasn't a person
    talking to Avi (see the module docstring) and so left no row.
    """
    settings = get_settings()
    try:
        avi = _avi(settings)
    except slack.SlackReadError as exc:
        logger.error(
            "Slack inbox: can't establish Avi's Slack identity (%s); "
            "event %s not processed.",
            exc,
            payload.get("event_id"),
        )
        return None

    parsed = slack.parse_event(
        payload,
        bot_user_id=avi.bot_user_id,
        human_user_id=avi.human_user_id,
        app_id=settings.SLACK_APP_ID,
        team_id=avi.team_id,
        resolve_user=_name_resolver(settings),
    )
    if isinstance(parsed, slack.SkippedEvent):
        logger.info(
            "Slack inbox: not acting on event %s — %s",
            payload.get("event_id"),
            parsed.reason,
        )
        return None
    return process_inbound_message(db, parsed)


def process_inbound_message(
    db: Session, inbound: slack.InboundSlackMessage
) -> Optional[models.SlackInboxMessage]:
    """Resolve one Slack message into an audit row + (maybe) a reply.

    Mirrors :func:`api.services.telegram_inbox.process_inbound_update`
    step for step.
    """
    settings = get_settings()

    # ---- Dedup ------------------------------------------------------
    existing = _existing(db, inbound)
    if existing is not None:
        logger.info(
            "Slack inbox: %s/%s already handled (row %s); skipping redelivery.",
            inbound.channel_id,
            inbound.ts,
            existing.slack_inbox_message_id,
        )
        return existing

    # ---- Who sent it ------------------------------------------------
    try:
        sender = _lookup_user(settings, inbound.user_id)
    except slack.SlackReadError as exc:
        # No row on purpose: without the sender's email we can't decide
        # anything, and leaving the message unrecorded lets the catch-up
        # pass pick it up once Slack is reachable again.
        logger.error(
            "Slack inbox: couldn't look up sender %s for %s/%s (%s); "
            "left for the catch-up pass.",
            inbound.user_id,
            inbound.channel_id,
            inbound.ts,
            exc,
        )
        return None

    common = dict(
        slack_team_id=inbound.team_id,
        slack_channel_id=inbound.channel_id,
        slack_message_ts=inbound.ts,
        slack_thread_ts=inbound.thread_ts,
        slack_event_id=inbound.event_id,
        channel_type=inbound.channel_type,
        via=inbound.via,
        slack_user_id=inbound.user_id,
        sender_email=sender.email,
        sender_display_name=sender.display_name,
        body=inbound.body,
        num_media=inbound.num_media,
        received_at=_utcnow(),
    )

    person, refusal = _lookup_family_member(db, sender, team_id=inbound.team_id)
    if person is None:
        logger.info(
            "Slack inbox: ignoring unknown sender %s (%s/%s): %s",
            inbound.user_id,
            inbound.channel_id,
            inbound.ts,
            refusal,
        )
        audit, _created = _claim(
            db,
            family_id=None,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=refusal,
            **common,
        )
        return audit

    # ---- Open / reuse the Slack session -----------------------------
    session, _created = live_session.find_or_create_slack_session(
        db,
        family_id=person.family_id,
        channel_id=inbound.channel_id,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)
    history = live_session.conversation_history(db, session)

    # Claim the message before doing any work. The row starts as
    # 'failed' so a crash further down leaves a trail, and the unique
    # key means a concurrent redelivery loses here and stops.
    audit, created = _claim(
        db,
        family_id=person.family_id,
        person_id=person.person_id,
        status="failed",
        status_reason="Pipeline started but did not complete; see logs.",
        live_session_id=session.live_session_id,
        **common,
    )
    if not created:
        return audit

    # ---- Download shared files (if any) and persist + describe ------
    attachments_meta, rendered_attachments, over_cap, file_problems = (
        _persist_attachments(db, audit, inbound, settings)
    )
    attachment_block = vision.render_attachments_for_prompt(
        rendered_attachments, extras_omitted=over_cap
    )
    if attachments_meta:
        db.commit()

    # ---- Log inbound to the transcript ------------------------------
    live_session.log_message(
        db,
        session,
        role="user",
        content=_format_inbound_for_log(
            inbound, sender, attachments_meta, attachment_block=attachment_block
        ),
        person_id=person.person_id,
        meta={
            "kind": "slack",
            "slack_channel_id": inbound.channel_id,
            "slack_message_ts": inbound.ts,
            "slack_thread_ts": inbound.thread_ts,
            "slack_user_id": inbound.user_id,
            "via": inbound.via,
            "attachments": attachments_meta or None,
        },
    )

    # ---- Skip the agent loop if Slack is switched off ---------------
    if not settings.AI_SLACK_INBOUND_ENABLED:
        audit.status = "failed"
        audit.status_reason = "AI_SLACK_INBOUND_ENABLED=false"
        db.commit()
        return audit

    # ---- Run the agent ---------------------------------------------
    task = agent_loop.create_task(
        db,
        family_id=person.family_id,
        live_session_id=session.live_session_id,
        person_id=person.person_id,
        kind="slack",
        input_text=inbound.body or "",
        model=llm._model(),
    )
    audit.agent_task_id = task.agent_task_id
    db.commit()

    user_message = _format_user_message_for_agent(
        inbound,
        person,
        sender,
        attachment_block=attachment_block,
        file_problems=file_problems,
    )
    assistant_id = _assistant_id_for_family(db, person.family_id)

    # ---- Instant heuristic ack (two-message pattern) ---------------
    ack_text = fast_ack.heuristic_ack(inbound.body)
    ack_ts: Optional[str] = None
    if ack_text and settings.AI_FAST_ACK_ENABLED:
        try:
            ack_ts = _send(settings, inbound, ack_text)
            logger.info(
                "[orch] surface=slack instant_ack ts=%s ack_ts=%s text=%r",
                inbound.ts,
                ack_ts,
                ack_text,
            )
            live_session.log_message(
                db,
                session,
                role="assistant",
                content=ack_text,
                person_id=person.person_id,
                meta={
                    "kind": "slack_fast_ack",
                    "agent_task_id": task.agent_task_id,
                    "slack_channel_id": inbound.channel_id,
                    "slack_message_ts": ack_ts,
                    "in_reply_to": inbound.ts,
                },
            )
            audit.status_reason = f"Instant ack sent (ts={ack_ts}); awaiting reply."
            db.commit()
        except slack.SlackSendError as exc:
            logger.warning(
                "[orch] surface=slack instant_ack send failed ts=%s: %s "
                "(continuing with primary reply)",
                inbound.ts,
                exc,
            )

    # ---- Unified triage shortcut -----------------------------------
    # One fast-model call decides between web-search / family-QA /
    # action-intent / heavy. Files and open attachment questions always
    # go to the heavy agent — the fast paths have no attachment tools.
    shortcut_used: Optional[str] = None
    agent_failed = False
    final_text: Optional[str] = None
    open_questions = attachment_intake.open_questions(db, person_id=person.person_id)
    needs_heavy = bool(inbound.files) or bool(open_questions)
    triage = fast_action_router.try_triage_sync(
        (inbound.body or "").strip(),
        family_id=person.family_id,
        person_id=person.person_id,
        assistant_id=assistant_id,
        live_session_id=session.live_session_id,
        surface_label="Slack",
        agent_task_kind="slack",
        allow_action=not needs_heavy,
        allow_family_qa=not needs_heavy,
    )
    if triage.reply:
        logger.info(
            "[orch] surface=slack path=%s ts=%s task=%s person_id=%s "
            "reply_chars=%d (skipping heavy agent)",
            triage.used_path,
            inbound.ts,
            task.agent_task_id,
            person.person_id,
            len(triage.reply),
        )
        final_text = triage.reply
        shortcut_used = triage.used_path

    if final_text is None:
        logger.info(
            "[orch] surface=slack path=heavy_agent ts=%s task=%s "
            "person_id=%s n_attachments=%d instant_acked=%s",
            inbound.ts,
            task.agent_task_id,
            person.person_id,
            len(attachments_meta),
            bool(ack_ts),
        )
        system_prompt = _build_slack_system_prompt(
            db,
            family_id=person.family_id,
            person=person,
            inbound=inbound,
            sender_display_name=sender.display_name or sender.email or "a family member",
        )
        inbound_attachment_refs = [
            agent_tools.InboundAttachmentRef(
                media_index=m["media_index"],
                filename=m["filename"],
                mime_type=m.get("mime_type"),
                size_bytes=m.get("file_size_bytes"),
                stored_path=m["stored_path"],
                channel="slack",
            )
            for m in attachments_meta
        ]
        intake_block = attachment_intake.auto_file_attachments(
            db,
            family_id=person.family_id,
            sender=person,
            message_text=inbound.body,
            refs=inbound_attachment_refs,
        )
        followup_refs, followup_block = attachment_intake.followup_for_agent(
            db, open_questions, first_index=len(inbound_attachment_refs) + 1
        )
        inbound_attachment_refs += followup_refs
        for block in (intake_block, followup_block):
            if block:
                user_message = f"{user_message}\n\n{block}"

        # Every message from a family member gets a reply, even when
        # the agent itself falls over.
        try:
            final_text = _run_agent_to_completion(
                task_id=task.agent_task_id,
                family_id=person.family_id,
                assistant_id=assistant_id,
                person_id=person.person_id,
                system_prompt=system_prompt,
                history=history,
                user_message=user_message,
                inbound_attachments=inbound_attachment_refs,
                shared_conversation=not inbound.is_direct,
            )
        except Exception:  # noqa: BLE001 - last-ditch catch so we always reply
            logger.exception(
                "Slack inbox: agent loop crashed for %s/%s task=%s",
                inbound.channel_id,
                inbound.ts,
                task.agent_task_id,
            )
            agent_failed = True
            final_text = (
                "Got your message — Avi here. I hit a snag on my end "
                "and couldn't finish that just now. I'll look into it."
            )

    final_text = slack.truncate_for_slack(
        final_text or "Got your message — nothing to add right now.",
        max_chars=settings.AI_SLACK_REPLY_MAX_CHARS,
    )

    # ---- Files the agent attached to this reply ---------------------
    # Only the heavy agent can attach files (the ``send_file`` tool).
    reply_files = outbound_files.ReplyFiles()
    if shortcut_used is None and not agent_failed:
        reply_files = outbound_files.collect_for_task(
            db,
            task_id=task.agent_task_id,
            family_id=person.family_id,
            channel="slack",
        )
        if reply_files.problems:
            final_text = (
                f"{final_text}\n\n{outbound_files.failure_note([], reply_files.problems)}"
            )

    # ---- Send the (final) reply ------------------------------------
    try:
        reply_ts = _send(settings, inbound, final_text)
    except slack.SlackSendError as exc:
        logger.exception(
            "Slack inbox: reply failed for %s/%s", inbound.channel_id, inbound.ts
        )
        audit.status = "failed"
        audit.status_reason = f"chat.postMessage: {exc}"
        db.commit()
        return audit

    files_sent, files_failed = _send_files(settings, inbound, reply_files.files)
    if files_failed:
        # The reply said a file was coming; say so when it isn't.
        try:
            _send(settings, inbound, outbound_files.failure_note(files_failed))
        except slack.SlackSendError:
            logger.exception(
                "Slack inbox: could not report failed file upload for %s/%s",
                inbound.channel_id,
                inbound.ts,
            )

    # ---- Log reply + flip audit row to success ----------------------
    live_session.log_message(
        db,
        session,
        role="assistant",
        content=final_text,
        person_id=person.person_id,
        meta={
            "kind": "slack_reply",
            "agent_task_id": task.agent_task_id,
            "slack_channel_id": inbound.channel_id,
            "slack_message_ts": reply_ts,
            "in_reply_to": inbound.ts,
            "via": inbound.via,
            "ack_slack_message_ts": ack_ts,
            **({"shortcut": shortcut_used} if shortcut_used else {}),
            **({"attachments_sent": files_sent} if files_sent else {}),
            **({"attachments_failed": files_failed} if files_failed else {}),
        },
    )
    notes: List[str] = []
    if agent_failed:
        notes.append("Agent loop crashed; sent fallback apology.")
    if files_sent:
        notes.append(
            "Sent file(s): " + ", ".join(f["label"] for f in files_sent) + "."
        )
    if files_failed or reply_files.problems:
        notes.append(
            "File(s) not sent: "
            + "; ".join([*files_failed, *reply_files.problems])
        )
    if file_problems:
        notes.append(
            f"{len(file_problems)} shared file(s) could not be retrieved: "
            + "; ".join(file_problems)
        )
    audit.status = "processed_replied"
    audit.status_reason = " ".join(notes) or None
    audit.reply_message_ts = reply_ts
    db.commit()
    return audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send(
    settings: Settings, inbound: slack.InboundSlackMessage, text: str
) -> str:
    """Post ``text`` back into the conversation, as the identity it reached."""
    token = _token_for(inbound.via, settings)
    if not token:
        raise slack.SlackSendError(
            f"No Slack {inbound.via} token configured to reply with."
        )
    return slack.post_message(
        token=token,
        channel=inbound.channel_id,
        text=slack.format_outbound(text, max_chars=settings.AI_SLACK_REPLY_MAX_CHARS),
        thread_ts=inbound.reply_thread_ts,
    )


def _send_files(
    settings: Settings,
    inbound: slack.InboundSlackMessage,
    files: List[outbound_files.OutboundFile],
) -> Tuple[List[dict], List[str]]:
    """Upload ``files`` into the conversation, under the reply.

    Returns what was sent (for the transcript) and the labels of
    anything that failed. One failed upload doesn't stop the others.
    """
    sent: List[dict] = []
    failed: List[str] = []
    if not files:
        return sent, failed
    token = _token_for(inbound.via, settings)
    for file in files:
        try:
            if not token:
                raise slack.SlackSendError(
                    f"No Slack {inbound.via} token configured to reply with."
                )
            prepared = outbound_files.prepare(file, "slack")
            file_id = slack.upload_file(
                token=token,
                channel=inbound.channel_id,
                filename=prepared.filename,
                data=prepared.data,
                title=file.label,
                thread_ts=inbound.reply_thread_ts,
            )
            sent.append({**file.describe(), "slack_file_id": file_id})
        except (slack.SlackSendError, outbound_files.FileUnavailable) as exc:
            logger.warning(
                "Slack inbox: file upload failed for %s/%s (%s:%s): %s",
                inbound.channel_id,
                inbound.ts,
                file.source,
                file.record_id,
                exc,
            )
            failed.append(file.label)
    return sent, failed


def _existing(
    db: Session, inbound: slack.InboundSlackMessage
) -> Optional[models.SlackInboxMessage]:
    return db.execute(
        select(models.SlackInboxMessage)
        .where(models.SlackInboxMessage.slack_team_id == inbound.team_id)
        .where(models.SlackInboxMessage.slack_channel_id == inbound.channel_id)
        .where(models.SlackInboxMessage.slack_message_ts == inbound.ts)
        .limit(1)
    ).scalar_one_or_none()


def _claim(db: Session, **kwargs) -> Tuple[models.SlackInboxMessage, bool]:
    """Insert the audit row. ``(row, False)`` when another worker got there first."""
    row = models.SlackInboxMessage(**kwargs)
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(models.SlackInboxMessage)
            .where(models.SlackInboxMessage.slack_team_id == kwargs["slack_team_id"])
            .where(
                models.SlackInboxMessage.slack_channel_id == kwargs["slack_channel_id"]
            )
            .where(
                models.SlackInboxMessage.slack_message_ts == kwargs["slack_message_ts"]
            )
            .limit(1)
        ).scalar_one()
        return existing, False


def _lookup_family_member(
    db: Session, sender: slack.SlackUser, *, team_id: str
) -> Tuple[Optional[models.Person], Optional[str]]:
    """Match a Slack account to a household member by its email.

    Returns ``(person, None)`` or ``(None, why_not)``. Same rule as the
    email inbox: an exact, case-insensitive match on the person's own
    email or on the work email of one of their jobs. When two people
    share an address the lowest ``person_id`` wins, as with phones.
    """
    if sender.is_bot or sender.deleted:
        return None, "Sender is a bot or deactivated Slack account."
    if sender.team_id and team_id and sender.team_id != team_id:
        return None, (
            f"Sender belongs to another Slack workspace ({sender.team_id}); "
            "only members of Avi's own workspace are recognised."
        )
    email = (sender.email or "").strip().lower()
    if not email:
        return None, (
            "Slack did not share an email for this account (the app needs "
            "the users:read.email scope, or the account has none)."
        )
    job_email_match = (
        select(models.Job.person_id)
        .where(models.Job.person_id == models.Person.person_id)
        .where(func.lower(models.Job.work_email) == email)
        .exists()
    )
    person = db.execute(
        select(models.Person)
        .where((func.lower(models.Person.email_address) == email) | job_email_match)
        .order_by(models.Person.person_id)
        .limit(1)
    ).scalar_one_or_none()
    if person is None:
        return None, f"No Person.email_address in any family matches {email!r}."
    return person, None


def _persist_attachments(
    db: Session,
    audit: models.SlackInboxMessage,
    inbound: slack.InboundSlackMessage,
    settings: Settings,
) -> Tuple[List[dict], List[vision.RenderableAttachment], int, List[str]]:
    """Download shared files, persist rows, and run the vision pipeline.

    Returns ``(meta, rendered, over_cap, problems)``. The first three
    match the SMS / Telegram helpers. ``problems`` names every file we
    could not retrieve and why, so the agent can tell the sender rather
    than answer as if nothing had been attached. One bad file never
    blocks the others.
    """
    if not inbound.files or audit.family_id is None:
        return [], [], 0, []
    token = _token_for(inbound.via, settings) or ""
    meta: List[dict] = []
    inputs: List[vision.AttachmentInput] = []
    problems: List[str] = []
    for ref in inbound.files:
        if not ref.url:
            problems.append(f"{ref.name}: Slack gave no download link")
            continue
        if ref.size_bytes and ref.size_bytes > settings.AI_ATTACHMENT_MAX_BYTES:
            problems.append(
                f"{ref.name}: too large "
                f"({ref.size_bytes // (1024 * 1024)} MB, limit "
                f"{settings.AI_ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB)"
            )
            continue
        try:
            blob = slack.download_file(
                token=token, url=ref.url, hint_mime=ref.mime_type
            )
        except slack.SlackReadError as exc:
            logger.warning(
                "Slack inbox: failed to fetch file %s (%s) for %s/%s: %s",
                ref.file_id,
                ref.name,
                inbound.channel_id,
                inbound.ts,
                exc,
            )
            problems.append(f"{ref.name}: {exc}")
            continue
        mime = (
            ref.mime_type
            if ref.mime_type and ref.mime_type != "application/octet-stream"
            else blob.mime_type
        )
        filename = ref.name[:255]
        rel_path, size = storage.save_slack_attachment(
            family_id=audit.family_id,
            slack_inbox_message_id=audit.slack_inbox_message_id,
            file_bytes=blob.file_bytes,
            extension=slack.extension_for_mime(mime),
            original_filename=filename,
        )
        db.add(
            models.SlackInboxAttachment(
                slack_inbox_message_id=audit.slack_inbox_message_id,
                media_index=ref.media_index,
                slack_file_id=ref.file_id,
                filename=filename,
                mime_type=mime,
                file_size_bytes=size,
                stored_path=rel_path,
            )
        )
        meta.append(
            {
                "media_index": ref.media_index,
                "mime_type": mime,
                "stored_path": rel_path,
                "file_size_bytes": size,
                "filename": filename,
            }
        )
        inputs.append(
            vision.AttachmentInput(
                index=ref.media_index,
                path=settings.storage_root / rel_path,
                filename=filename,
                mime_type=mime,
                size_bytes=size,
            )
        )
    if meta:
        db.flush()
    rendered, over_cap = vision.describe_many(
        inputs, max_to_describe=settings.AI_ATTACHMENT_MAX_PER_MESSAGE
    )
    return meta, rendered, over_cap, problems


def _where(inbound: slack.InboundSlackMessage) -> str:
    if inbound.is_direct:
        return "direct message"
    return f"channel {inbound.channel_id}"


def _format_inbound_for_log(
    inbound: slack.InboundSlackMessage,
    sender: slack.SlackUser,
    attachments: List[dict],
    *,
    attachment_block: str = "",
) -> str:
    """Render an inbound Slack message so it reads naturally in history."""
    lines = [
        f"From: {sender.display_name or sender.email or inbound.user_id}",
        f"Slack: {_where(inbound)}",
        "",
        inbound.body or "(no text)",
    ]
    if attachment_block:
        lines.append("")
        lines.append(attachment_block)
    elif attachments:
        lines.append("")
        for att in attachments:
            lines.append(
                f"[attachment {att['media_index']}: {att['filename']} "
                f"({att['mime_type']}, {att['file_size_bytes']} bytes)]"
            )
    return "\n".join(lines)


def _format_user_message_for_agent(
    inbound: slack.InboundSlackMessage,
    person: models.Person,
    sender: slack.SlackUser,
    *,
    attachment_block: str = "",
    file_problems: Optional[List[str]] = None,
) -> str:
    """Wrap the Slack message so the agent knows the surface."""
    name = person.preferred_name or person.first_name or sender.display_name or "?"
    media_hint = ""
    if inbound.num_media:
        noun = "file" if inbound.num_media == 1 else "files"
        media_hint = f" (with {inbound.num_media} attached {noun})"
    surface = (
        "Slack message" if inbound.is_direct else "Slack channel message mentioning you"
    )
    parts = [
        f"[{surface} from {name}{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no text)'}"
    ]
    if attachment_block:
        parts.append(attachment_block)
    if file_problems:
        parts.append(
            "--- Files you could NOT open ---\n"
            + "\n".join(f"- {p}" for p in file_problems)
            + "\nYou have not seen these files. Say so, and ask the sender "
            "to send them another way if they matter."
        )
    return "\n\n".join(parts)


def _build_slack_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    inbound: slack.InboundSlackMessage,
    sender_display_name: str,
) -> str:
    """Build the Slack-flavoured system prompt.

    The shared scaffolding (who Avi is, the household, the speaker's
    privacy scope, tool hints) comes from
    :func:`inbound_prompts.build_inbound_system_prompt`; only the
    surface verb and the "how to reply" block are Slack's.
    """
    max_chars = get_settings().AI_SLACK_REPLY_MAX_CHARS
    if inbound.is_direct:
        arrived = f"as a private Slack direct message from {sender_display_name}"
        audience = ""
    else:
        arrived = (
            f"in a shared Slack channel, where {sender_display_name} mentioned you"
        )
        audience = (
            "* Everyone in that channel can read your reply. If the answer "
            "  involves anything private (ID or account numbers, medical or "
            "  financial details), don't post it — ask them to message you "
            "  directly instead.\n"
        )
    return inbound_prompts.build_inbound_system_prompt(
        db,
        family_id=family_id,
        person=person,
        surface_verb="chatting with on Slack",
        assistant_id=_assistant_id_for_family(db, family_id),
        how_to_reply=(
            "--- How to reply to this Slack message ---\n"
            f"This message arrived {arrived}. Your final answer will be "
            "posted verbatim as your Slack reply. Therefore:\n"
            f"* Keep the reply UNDER {max_chars} characters. Slack is a "
            "  chat: a few short paragraphs at most.\n"
            "* Write plain text. Short '- ' lists and *bold* for a key "
            "  word are fine; no tables, no headings.\n"
            "* No subject line, no greeting, no formal sign-off. The "
            "  recipient knows who you are; jump straight to the answer.\n"
            "* Use at most one round of tool calls before writing the "
            "  reply. Slack is asynchronous — if you need more info, "
            "  ask the user one specific question and stop.\n"
            f"{audience}"
            "* NEVER include encrypted identifiers, passwords, or anything "
            "  ending in _encrypted in the reply body.\n"
        ),
    )


_assistant_id_for_family = _assistants.assistant_id_for_family


def _run_agent_to_completion(
    *,
    task_id: int,
    family_id: int,
    assistant_id: Optional[int],
    person_id: int,
    system_prompt: str,
    history: List[dict],
    user_message: str,
    inbound_attachments: Optional[List[agent_tools.InboundAttachmentRef]] = None,
    shared_conversation: bool = False,
) -> str:
    """Drain the async agent generator and return the final reply text.

    Thin wrapper over :func:`api.ai.agent_drain.drain_agent_sync`, the
    same one the SMS and Telegram inboxes use. ``shared_conversation``
    is true for a channel other people can read, where private files
    must not be posted.
    """
    registry = agent_tools.build_default_registry()
    with SessionLocal() as db_for_caps:
        capabilities = agent_tools.detect_capabilities(db_for_caps, assistant_id)

    # Synthetic capability that exposes ``task_attach_message_attachment``
    # only on turns that actually carry a file.
    if inbound_attachments:
        capabilities = set(capabilities) | {"inbound_attachment"}

    result = agent_drain.drain_agent_sync(
        task_id=task_id,
        family_id=family_id,
        assistant_id=assistant_id,
        person_id=person_id,
        system_prompt=system_prompt,
        history=history,
        user_message=user_message,
        registry=registry,
        capabilities=capabilities,
        extra_run_kwargs={
            "inbound_attachments": list(inbound_attachments or []),
            "reply_is_shared": shared_conversation,
        },
    )

    if result.error_text and not result.final_text:
        return (
            "Sorry — Avi here. I tried but my server hit a snag. "
            "Try again in a bit."
        )
    return result.final_text or "Got your message — nothing to add right now."


# ---------------------------------------------------------------------------
# Downtime catch-up
# ---------------------------------------------------------------------------


def replay_missed_messages(*, lookback_hours: int, max_messages: int) -> int:
    """Answer direct messages that arrived while the webhook was unreachable.

    Slack retries a failed delivery three times over a few minutes and
    then gives up; the message stays in the conversation. For every DM
    a family member has already used with Avi, this re-reads the last
    ``lookback_hours`` of it and runs anything we have no record of
    through the normal pipeline. Returns how many messages were handed
    to it.

    It never reaches back before the first message Avi recorded in a
    conversation, so turning Slack on doesn't make him answer old
    history. That also means a conversation with no recorded message
    yet isn't checked, and neither are channel mentions.
    """
    settings = get_settings()
    if not settings.AI_SLACK_INBOUND_ENABLED or not (
        settings.SLACK_BOT_TOKEN or settings.SLACK_USER_TOKEN
    ):
        return 0
    try:
        avi = _avi(settings)
    except slack.SlackReadError as exc:
        logger.warning("Inbound recovery: Slack identity check failed: %s", exc)
        return 0

    since_ts = (
        _utcnow() - timedelta(hours=max(1, lookback_hours))
    ).timestamp()
    Msg = models.SlackInboxMessage
    with SessionLocal() as db:
        conversations = db.execute(
            # Slack timestamps are fixed-width ("1732468800.123456"), so
            # the smallest string is the earliest message.
            select(Msg.slack_channel_id, Msg.via, func.min(Msg.slack_message_ts))
            .where(Msg.slack_team_id == avi.team_id)
            .where(Msg.channel_type == "im")
            .where(Msg.person_id.is_not(None))
            .group_by(Msg.slack_channel_id, Msg.via)
        ).all()

    resolve_user = _name_resolver(settings)
    replayed = 0
    for channel_id, via, first_ts in conversations:
        if replayed >= max_messages:
            break
        token = _token_for(via, settings)
        if not token:
            continue
        try:
            first_is_recent = float(first_ts) >= since_ts
        except (TypeError, ValueError):
            first_is_recent = False
        oldest = str(first_ts) if first_is_recent else f"{since_ts:.6f}"
        try:
            messages = slack.conversations_history(
                token=token, channel=channel_id, oldest=oldest
            )
        except slack.SlackReadError as exc:
            logger.warning(
                "Inbound recovery: Slack history for %s failed: %s", channel_id, exc
            )
            continue
        for raw in messages:
            if replayed >= max_messages:
                break
            parsed = slack.parse_message(
                raw,
                team_id=avi.team_id,
                via=via,
                bot_user_id=avi.bot_user_id,
                human_user_id=avi.human_user_id,
                app_id=settings.SLACK_APP_ID,
                channel_id=channel_id,
                channel_type="im",
                resolve_user=resolve_user,
            )
            if isinstance(parsed, slack.SkippedEvent):
                continue
            try:
                with SessionLocal() as db:
                    if _existing(db, parsed) is not None:
                        continue
                    process_inbound_message(db, parsed)
                replayed += 1
            except Exception:  # noqa: BLE001 - one bad message must not stop the rest
                logger.exception(
                    "Inbound recovery: failed to replay Slack %s/%s",
                    channel_id,
                    parsed.ts,
                )
    return replayed


__all__ = [
    "process_event",
    "process_inbound_message",
    "replay_missed_messages",
]
