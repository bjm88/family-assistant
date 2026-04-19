"""Telegram-driven AI assistant — long-poll loop + per-update agent dispatch.

Big picture
-----------
This module turns Avi into a **Telegram auto-responder for registered
family members only**. Architecturally it sits between the email
poller (also a long-poll background loop) and the SMS webhook (also a
synchronous per-message pipeline that always replies through a REST
API):

* A long-lived asyncio loop (:func:`run_telegram_inbox_loop`) calls
  the Bot API's ``getUpdates`` with a 25 s long-poll. Cancel with
  ``stop_event.set()`` — the loop wakes within ~1 s.
* Every actionable update (a normal text or media message in a
  private/group chat) is parsed and handed to
  :func:`process_inbound_update`, which does dedup, person lookup,
  session bookkeeping, agent dispatch, and reply send — same shape as
  ``services.sms_inbox.process_inbound_sms`` and
  ``services.email_inbox._handle_one_message``.
* The sender's Telegram user id is matched against
  ``people.telegram_user_id`` (and falls back to
  ``people.telegram_username``) for every family in the database. **All
  unmatched senders are silently ignored, recorded in
  ``telegram_inbox_messages`` with ``status='ignored_unknown_sender'``.**
  This is the single security gate — no other code path replies to
  Telegram.
* When a sender does match a registered person we open / reuse a
  ``LiveSession`` keyed on the Telegram chat id, log the inbound
  message into the transcript, run the same agent loop the live chat
  uses, and send the final answer back via ``sendMessage``.

What this code DELIBERATELY does not do
---------------------------------------
* Reply to anyone whose ``message.from.id`` (or ``@username``) does not
  match a registered :class:`api.models.Person`. Strangers see the
  bot stay silent.
* Reply to its own outbound traffic (loopback). If an inbound's
  ``from.id`` matches our bot's own id it's recorded as
  ``ignored_self`` and dropped.
* React to non-message updates. Edits, channel posts, callback
  queries, etc. land as ``ignored_non_message``.

Failure isolation
-----------------
A crash on one update never stops the loop; the per-update handler
records a ``failed`` audit row so the operator can see what went
wrong from the admin UI without ssh'ing in to read logs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from concurrent.futures import TimeoutError as FuturesTimeoutError

from .. import models, storage
from ..ai import agent as agent_loop
from ..ai import authz
from ..ai import fast_ack
from ..ai import ollama, prompts, rag, schema_catalog
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..config import get_settings
from ..db import SessionLocal
from ..integrations import telegram, twilio_sms
from ..utils.phone import normalize_phone
from . import background_agent


logger = logging.getLogger(__name__)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Long-poll loop
# ---------------------------------------------------------------------------


async def run_telegram_inbox_loop(stop_event: asyncio.Event) -> None:
    """Forever-running long-poll loop. Cancel with ``stop_event.set()``.

    Long-polls the Bot API for new updates and dispatches each one to
    :func:`process_inbound_update` on a worker thread (so the agent's
    blocking ``asyncio.run`` doesn't collide with our own event loop).
    """
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning(
            "Telegram inbox loop: TELEGRAM_BOT_TOKEN not set — staying idle. "
            "Add it to .env to enable Avi's Telegram autopilot."
        )
        return

    bot_token = settings.TELEGRAM_BOT_TOKEN

    # Best-effort getMe so we can short-circuit self-loops. If the
    # network is flaky on startup we just retry on the next tick — no
    # need to crash the poller.
    bot_user_id: Optional[int] = None
    bot_username: Optional[str] = None
    try:
        identity = await asyncio.to_thread(telegram.get_me, bot_token)
        bot_user_id = identity.user_id or None
        bot_username = identity.username
        logger.info(
            "Telegram inbox loop starting (bot=@%s, longpoll=%ds, max_per_tick=%d)",
            bot_username or "?",
            settings.AI_TELEGRAM_LONGPOLL_SECONDS,
            settings.AI_TELEGRAM_INBOX_MAX_PER_TICK,
        )
    except telegram.TelegramReadError as exc:
        logger.warning(
            "Telegram inbox loop: getMe failed (%s) — will retry on first tick.",
            exc,
        )

    # We persist offset state purely in memory: the next call passes
    # ``last_seen_update_id + 1`` so Telegram drops everything we've
    # already audited. The dedup uniqueness constraint on
    # ``telegram_update_id`` makes restarts safe even if we briefly
    # double-fetch an update we already wrote.
    next_offset: Optional[int] = await asyncio.to_thread(
        _initial_offset_from_db
    )

    while not stop_event.is_set():
        try:
            updates = await asyncio.to_thread(
                telegram.get_updates,
                bot_token=bot_token,
                offset=next_offset,
                timeout_seconds=settings.AI_TELEGRAM_LONGPOLL_SECONDS,
                limit=settings.AI_TELEGRAM_INBOX_MAX_PER_TICK,
            )
        except telegram.TelegramReadError as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            await _sleep_with_stop(5.0, stop_event)
            continue
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Telegram inbox loop crashed; backing off 5s")
            await _sleep_with_stop(5.0, stop_event)
            continue

        if not updates:
            # getUpdates returned an empty array because the long-poll
            # window expired with nothing new. Just loop straight back
            # in — no need to sleep, the long-poll itself was the wait.
            continue

        for raw_update in updates:
            try:
                next_offset = max(
                    next_offset or 0, int(raw_update.get("update_id") or 0)
                ) + 1
            except (TypeError, ValueError):
                pass

            try:
                await asyncio.to_thread(
                    _dispatch_one_update,
                    raw_update,
                    bot_user_id,
                )
            except Exception:  # noqa: BLE001 - per-update isolation
                logger.exception(
                    "Telegram inbox: per-update dispatch crashed (update_id=%s)",
                    raw_update.get("update_id"),
                )

    logger.info("Telegram inbox loop stopped.")


async def _sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep ``seconds`` but wake immediately when ``stop_event`` is set."""
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(1.0, remaining))
            return
        except asyncio.TimeoutError:
            continue


def _initial_offset_from_db() -> Optional[int]:
    """Return ``last_seen_update_id + 1`` from the DB, if any.

    Lets a restart pick up exactly where the previous process left off
    without re-replying to messages it already audited. We rely on the
    dedup uniqueness constraint as a backstop; this query is just a
    perf optimisation to avoid Telegram resending hundreds of old
    updates after a long downtime.
    """
    with SessionLocal() as db:
        row = db.execute(
            select(models.TelegramInboxMessage.telegram_update_id)
            .order_by(models.TelegramInboxMessage.telegram_update_id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return (row + 1) if row is not None else None


def _dispatch_one_update(raw_update: dict, bot_user_id: Optional[int]) -> None:
    """Open a fresh DB session and run the per-update pipeline."""
    with SessionLocal() as db:
        process_inbound_update(db, raw_update, bot_user_id=bot_user_id)


# ---------------------------------------------------------------------------
# Per-update pipeline
# ---------------------------------------------------------------------------


def process_inbound_update(
    db: Session,
    raw_update: dict,
    *,
    bot_user_id: Optional[int],
) -> Optional[models.TelegramInboxMessage]:
    """Resolve one Bot API update into an audit row + (maybe) a reply.

    Mirrors :func:`api.services.sms_inbox.process_inbound_sms` step
    for step. Returns the persisted audit row, or ``None`` when the
    update wasn't a message at all (we still log a row so the audit
    trail is complete).
    """
    settings = get_settings()
    update_id = int(raw_update.get("update_id") or 0)

    # ---- Non-message gate -------------------------------------------
    if not telegram.is_actionable_message_update(raw_update):
        return _save_audit(
            db,
            telegram_update_id=update_id,
            telegram_chat_id=0,
            telegram_message_id=0,
            family_id=None,
            person_id=None,
            status="ignored_non_message",
            status_reason=(
                "Update has no 'message' payload (edit, channel post, "
                "callback query, etc.)."
            ),
            received_at=_utcnow(),
        )

    inbound = telegram.parse_inbound_update(raw_update)

    common = dict(
        telegram_update_id=inbound.update_id,
        telegram_chat_id=inbound.chat_id,
        telegram_message_id=inbound.message_id,
        telegram_user_id=inbound.from_user_id,
        telegram_username=inbound.from_username,
        sender_display_name=inbound.sender_display_name,
        body=inbound.body,
        num_media=inbound.num_media,
        received_at=_utcnow(),
    )

    # ---- Dedup ------------------------------------------------------
    if _already_processed(db, inbound.update_id):
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_already_seen",
            status_reason=(
                "Telegram redelivered an update we already processed."
            ),
            **common,
        )

    # ---- Self-loop --------------------------------------------------
    if (
        inbound.is_bot_sender
        and bot_user_id is not None
        and inbound.from_user_id == bot_user_id
    ):
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_self",
            status_reason="Sender matches our own bot account.",
            **common,
        )

    # ---- Deep-link invite claim (/start <payload>) ------------------
    # Telegram bots can't initiate a conversation, so we let the agent
    # mint a one-time ``t.me/<bot>?start=<token>`` link and send it via
    # SMS or email. Tapping the link makes Telegram deliver
    # ``/start <token>`` as the very first message — exactly what we
    # consume here to bind the sender's Telegram identity to the
    # invited Person row before the standard person-lookup gate runs.
    invite_payload = _extract_start_payload(inbound.body)
    if invite_payload:
        claim_outcome = _claim_telegram_invite(
            db,
            payload_token=invite_payload,
            inbound=inbound,
            common_audit=common,
        )
        if claim_outcome is not None:
            return claim_outcome

    # ---- Auto-link via shared contact -------------------------------
    # The Telegram Bot API never exposes a sender's phone number on
    # its own — the only way it shows up is when the user explicitly
    # taps a ``request_contact`` keyboard button. When that happens,
    # the follow-up message arrives with ``message.contact`` populated;
    # we use the shared phone number to find a unique Person row and
    # bind the Telegram identity to it. Same end-state as a /start
    # invite claim, just without the household admin needing to mint
    # a link first.
    if inbound.is_contact_share:
        contact_outcome = _try_link_via_shared_contact(
            db,
            inbound=inbound,
            common_audit=common,
        )
        if contact_outcome is not None:
            return contact_outcome

    # ---- Two-factor verification of a previously-shared contact -----
    # If we previously asked this chat to confirm a Twilio-delivered
    # 6-digit code, every plain-text message they send is potentially
    # a verification attempt. We check this BEFORE the regular person
    # lookup so an unbound sender mid-flow doesn't get misclassified
    # as an unknown stranger and re-prompted to share contact.
    pending_verification = _pending_verification_for_chat(
        db,
        chat_id=inbound.chat_id,
        telegram_user_id=inbound.from_user_id,
    )
    if pending_verification is not None:
        return _handle_verification_attempt(
            db,
            verification=pending_verification,
            inbound=inbound,
            common_audit=common,
        )

    # ---- Person lookup ----------------------------------------------
    person = _lookup_family_member_by_telegram(
        db,
        user_id=inbound.from_user_id,
        username=inbound.from_username,
    )
    # If we found the person via @username only, opportunistically
    # backfill the more-stable numeric id so subsequent messages can
    # short-circuit on the user_id branch (and keep working even if
    # the user later changes their @handle).
    if (
        person is not None
        and person.telegram_user_id is None
        and inbound.from_user_id is not None
    ):
        person.telegram_user_id = inbound.from_user_id
        db.flush()

    if person is None:
        return _handle_unknown_sender(
            db,
            inbound=inbound,
            common_audit=common,
        )

    # ---- Open / reuse the Telegram session --------------------------
    session, _created = live_session.find_or_create_telegram_session(
        db,
        family_id=person.family_id,
        chat_id=inbound.chat_id,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)

    # Audit row first (status='failed') so a downstream crash leaves a
    # forensic trail. We'll flip its status afterwards.
    audit = _save_audit(
        db,
        family_id=person.family_id,
        person_id=person.person_id,
        status="failed",
        status_reason="Pipeline started but did not complete; see logs.",
        live_session_id=session.live_session_id,
        **common,
    )

    # ---- Download attachments (if any) and persist + log ------------
    attachments_meta = _persist_attachments(db, audit, inbound)
    if attachments_meta:
        db.commit()

    # ---- Log inbound to the transcript ------------------------------
    inbound_text = _format_inbound_for_log(inbound, attachments_meta)
    live_session.log_message(
        db,
        session,
        role="user",
        content=inbound_text,
        person_id=person.person_id,
        meta={
            "kind": "telegram",
            "telegram_update_id": inbound.update_id,
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": inbound.message_id,
            "from_user_id": inbound.from_user_id,
            "from_username": inbound.from_username,
            "attachments": attachments_meta or None,
        },
    )

    # ---- Skip the agent loop if Telegram is disabled ---------------
    if not settings.AI_TELEGRAM_INBOUND_ENABLED:
        audit.status = "failed"
        audit.status_reason = "AI_TELEGRAM_INBOUND_ENABLED=false"
        db.commit()
        return audit

    # ---- Run the agent ---------------------------------------------
    task = agent_loop.create_task(
        db,
        family_id=person.family_id,
        live_session_id=session.live_session_id,
        person_id=person.person_id,
        kind="telegram",
        input_text=inbound.body or "",
        model=ollama._model(),
    )
    audit.agent_task_id = task.agent_task_id
    db.commit()

    system_prompt = _build_telegram_system_prompt(
        db,
        family_id=person.family_id,
        person=person,
        sender_display_name=inbound.sender_display_name
        or inbound.from_username
        or str(inbound.from_user_id or "?"),
    )
    user_message = _format_user_message_for_agent(inbound, person)
    assistant_id_for_family = _assistant_id_for_family(db, person.family_id)

    # ---- Race-and-ack pattern (see api.ai.fast_ack docstring) -------
    # Kick the heavy agent off on a background thread immediately so
    # we can either:
    #   (a) send a single reply if the agent finishes inside the
    #       AI_FAST_ACK_AFTER_SECONDS window (no ack noise for short
    #       answers), or
    #   (b) fire a quick contextual "I'm on it" ack from the fast
    #       model and follow up with the real answer when the heavy
    #       agent converges.
    # Either way the audit row reflects exactly what was sent.
    final_text, agent_failed, ack_sent_message_id = _run_agent_with_fast_ack(
        db,
        inbound=inbound,
        person=person,
        task_id=task.agent_task_id,
        assistant_id=assistant_id_for_family,
        system_prompt=system_prompt,
        user_message=user_message,
        session=session,
        audit=audit,
    )

    final_text = telegram.truncate_for_telegram(
        final_text or "Got your message — nothing to add right now.",
        max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
    )

    # ---- Send the (final) reply via the Bot API ---------------------
    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=final_text,
            reply_to_message_id=inbound.message_id,
        )
    except telegram.TelegramSendError as exc:
        logger.exception(
            "Telegram inbox: send_message failed for update_id=%s",
            inbound.update_id,
        )
        audit.status = "failed"
        audit.status_reason = f"send_message: {exc}"
        db.commit()
        return audit

    # ---- Log reply + flip audit row to success ----------------------
    live_session.log_message(
        db,
        session,
        role="assistant",
        content=final_text,
        person_id=person.person_id,
        meta={
            "kind": "telegram_reply",
            "agent_task_id": task.agent_task_id,
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": reply_message_id,
            "in_reply_to": inbound.message_id,
            # Cross-link to the ack message (if any) so an operator
            # reading the transcript can see both halves of the
            # exchange in the right order.
            "ack_telegram_message_id": ack_sent_message_id,
        },
    )
    audit.status = "processed_replied"
    if agent_failed:
        audit.status_reason = "Agent loop crashed; sent fallback apology."
    elif ack_sent_message_id is not None:
        audit.status_reason = (
            f"Sent fast-ack (msg={ack_sent_message_id}) then full reply "
            "after heavy agent converged."
        )
    else:
        audit.status_reason = None
    audit.reply_telegram_message_id = reply_message_id
    db.commit()
    return audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /start <payload> deep-link invite claim
# ---------------------------------------------------------------------------


def _extract_start_payload(body: Optional[str]) -> Optional[str]:
    """Return the ``<payload>`` part of ``/start <payload>`` if present.

    The Bot API guarantees that a deep-link tap arrives as a literal
    ``/start <payload>`` text message in a private chat; group chats
    use ``/start@<bot_username> <payload>`` instead. We accept both
    forms (and any optional trailing whitespace/newlines) and return
    the URL-safe payload Telegram preserved verbatim from the
    original ``?start=<payload>`` URL.
    """
    if not body:
        return None
    raw = body.strip()
    if not raw.startswith("/start"):
        return None
    # Lop off everything up to the first whitespace — that's "/start"
    # or "/start@bot_username".
    parts = raw.split(None, 1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    # Telegram caps deep-link payloads at 64 chars from the URL-safe
    # alphabet. Reject anything obviously not one of our tokens so a
    # human typing "/start now" doesn't trip the claim path.
    if not payload or len(payload) > 80:
        return None
    if any(ch.isspace() for ch in payload):
        return None
    return payload


def _claim_telegram_invite(
    db: Session,
    *,
    payload_token: str,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
) -> Optional[models.TelegramInboxMessage]:
    """Try to consume a ``telegram_invites`` row and bind the sender.

    Returns the audit row when the claim path handled the message
    (success OR a clean rejection like "already claimed"), or
    ``None`` to fall through to the normal person-lookup gate when
    the payload doesn't match an outstanding invite. Falling through
    matters for the case where the same sender already had their
    Telegram identity linked previously — we don't want a stale
    /start payload to break a working user.
    """
    invite = db.execute(
        select(models.TelegramInvite).where(
            models.TelegramInvite.payload_token == payload_token
        )
    ).scalar_one_or_none()

    if invite is None:
        # Unknown token — fall through to person lookup. If the sender
        # is already a registered family member they'll get a normal
        # reply; if not, they'll be silently ignored. Either way we
        # never reveal that an invite-token-shaped string failed to
        # match anything.
        return None

    invitee = db.get(models.Person, invite.person_id)
    if invitee is None:
        # Orphaned invite — the person was deleted. Audit + drop.
        return _save_audit(
            db,
            family_id=invite.family_id,
            person_id=None,
            status="failed",
            status_reason=(
                f"Telegram invite {invite.telegram_invite_id} points "
                "at a deleted person; refusing to claim."
            ),
            **common_audit,
        )

    # Reject claims on already-spent / revoked / expired invites.
    if not invite.is_outstanding():
        reason_bits = []
        if invite.claimed_at is not None:
            reason_bits.append(f"already_claimed_at={invite.claimed_at.isoformat()}")
        if invite.revoked_at is not None:
            reason_bits.append("revoked")
        if invite.expires_at and invite.expires_at <= _utcnow():
            reason_bits.append("expired")
        return _save_audit(
            db,
            family_id=invite.family_id,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=(
                "Telegram /start invite payload no longer valid: "
                + ", ".join(reason_bits or ["unknown"])
                + "."
            ),
            **common_audit,
        )

    # Anti-impersonation guard 1: the invite's person already has a
    # DIFFERENT Telegram identity bound. Refuse to overwrite — if the
    # household genuinely wants to swap channels they should clear the
    # old binding from the admin UI first.
    if (
        invitee.telegram_user_id is not None
        and inbound.from_user_id is not None
        and invitee.telegram_user_id != inbound.from_user_id
    ):
        return _save_audit(
            db,
            family_id=invite.family_id,
            person_id=invitee.person_id,
            status="ignored_unknown_sender",
            status_reason=(
                f"Telegram /start: person_id={invitee.person_id} is "
                f"already bound to telegram_user_id="
                f"{invitee.telegram_user_id}; refusing to rebind to "
                f"{inbound.from_user_id}."
            ),
            **common_audit,
        )

    # Anti-impersonation guard 2: the claiming Telegram identity is
    # already bound to a DIFFERENT person in the same household.
    # Common cause: someone forwarded the deep-link to a sibling.
    if inbound.from_user_id is not None:
        existing_owner = db.execute(
            select(models.Person)
            .where(models.Person.telegram_user_id == inbound.from_user_id)
            .where(models.Person.person_id != invitee.person_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing_owner is not None:
            return _save_audit(
                db,
                family_id=invite.family_id,
                person_id=existing_owner.person_id,
                status="ignored_unknown_sender",
                status_reason=(
                    f"Telegram /start: telegram_user_id="
                    f"{inbound.from_user_id} is already bound to "
                    f"person_id={existing_owner.person_id}; refusing "
                    f"to also bind it to person_id={invitee.person_id}."
                ),
                **common_audit,
            )

    # ---- Apply the binding -----------------------------------------
    invitee.telegram_user_id = inbound.from_user_id
    if inbound.from_username and not invitee.telegram_username:
        invitee.telegram_username = inbound.from_username
    invite.claimed_at = _utcnow()
    invite.claimed_telegram_user_id = inbound.from_user_id
    invite.claimed_telegram_username = inbound.from_username
    db.flush()

    # ---- Open / reuse a session so the welcome lands in history ----
    session, _created = live_session.find_or_create_telegram_session(
        db,
        family_id=invitee.family_id,
        chat_id=inbound.chat_id,
    )
    live_session.upsert_participant(db, session, person_id=invitee.person_id)

    audit = _save_audit(
        db,
        family_id=invitee.family_id,
        person_id=invitee.person_id,
        status="failed",
        status_reason="Invite-claim pipeline started but did not complete.",
        live_session_id=session.live_session_id,
        **common_audit,
    )

    live_session.log_message(
        db,
        session,
        role="user",
        content=_format_inbound_for_log(inbound, []),
        person_id=invitee.person_id,
        meta={
            "kind": "telegram_invite_claim",
            "telegram_update_id": inbound.update_id,
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": inbound.message_id,
            "from_user_id": inbound.from_user_id,
            "from_username": inbound.from_username,
            "telegram_invite_id": invite.telegram_invite_id,
        },
    )

    settings = get_settings()
    family = db.get(models.Family, invitee.family_id)
    assistant_name = (
        family.assistant.assistant_name
        if family and family.assistant
        else "Avi"
    )
    greeting = _build_invite_welcome(
        assistant_name=assistant_name,
        person=invitee,
    )
    greeting = telegram.truncate_for_telegram(
        greeting,
        max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
    )

    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=greeting,
            reply_to_message_id=inbound.message_id,
        )
    except telegram.TelegramSendError as exc:
        logger.exception(
            "Telegram invite-claim: send_message failed for update_id=%s",
            inbound.update_id,
        )
        audit.status = "failed"
        audit.status_reason = f"send_message: {exc}"
        db.commit()
        return audit

    live_session.log_message(
        db,
        session,
        role="assistant",
        content=greeting,
        person_id=invitee.person_id,
        meta={
            "kind": "telegram_invite_welcome",
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": reply_message_id,
            "in_reply_to": inbound.message_id,
            "telegram_invite_id": invite.telegram_invite_id,
        },
    )
    audit.status = "processed_replied"
    audit.status_reason = (
        f"Claimed telegram_invite_id={invite.telegram_invite_id}; "
        "bound Telegram identity and sent welcome."
    )
    audit.reply_telegram_message_id = reply_message_id
    db.commit()

    logger.info(
        "Telegram invite claimed: invite_id=%s person_id=%s "
        "telegram_user_id=%s @%s",
        invite.telegram_invite_id,
        invitee.person_id,
        inbound.from_user_id,
        inbound.from_username or "?",
    )
    return audit


def _build_invite_welcome(*, assistant_name: str, person: models.Person) -> str:
    """First message a freshly-bound Telegram user sees.

    Deliberately short — the recipient just tapped a link and is
    likely staring at a brand-new bot chat with no other context.
    Plain text only (no Markdown) since the Telegram inbox sends
    without ``parse_mode``.
    """
    name = person.preferred_name or person.first_name or "there"
    return (
        f"Hi {name} — {assistant_name} here. You're connected. "
        "Anything you send me from now on will go straight to me, "
        "exactly like the family chat or email. Ask me anything."
    )


# ---------------------------------------------------------------------------
# Auto-link by shared contact (request_contact button flow)
# ---------------------------------------------------------------------------


def _handle_unknown_sender(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
) -> models.TelegramInboxMessage:
    """Reject an inbound from a sender we can't identify.

    Two regimes, picked by the
    ``AI_TELEGRAM_AUTO_LINK_BY_PHONE`` setting:

    * **Auto-link enabled** (default) — if the chat is private AND
      we haven't recently prompted this chat, send a one-tap "Share
      my phone number" reply keyboard. That gives the sender a
      consent path to be auto-bound to their Person row without an
      out-of-band invite. Audit verdict:
      ``prompted_for_contact_share``.
    * **Auto-link disabled** OR the prompt would be redundant — fall
      back to the original silent-drop behaviour. Audit verdict:
      ``ignored_unknown_sender``.

    Returning the audit row lets the caller short-circuit the rest
    of :func:`process_inbound_update`.
    """
    settings = get_settings()

    silent_reason = (
        f"No Person.telegram_user_id={inbound.from_user_id!r} "
        f"or telegram_username={inbound.from_username!r} matched."
    )

    if not settings.AI_TELEGRAM_AUTO_LINK_BY_PHONE:
        logger.info(
            "Telegram inbox: ignoring unknown sender id=%s @%s "
            "(update_id=%s) — auto-link disabled",
            inbound.from_user_id,
            inbound.from_username or "?",
            inbound.update_id,
        )
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=silent_reason,
            **common_audit,
        )

    # Group chats: don't pop a reply keyboard at the whole room.
    if not inbound.is_private_chat:
        logger.info(
            "Telegram inbox: ignoring unknown sender in group chat "
            "id=%s (update_id=%s)",
            inbound.chat_id,
            inbound.update_id,
        )
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=(
                silent_reason
                + " (Skipped contact prompt: chat is not private.)"
            ),
            **common_audit,
        )

    # Cooldown — don't spam the same chat with prompts every time
    # they send "what?". The contact-share keyboard sticks around
    # client-side anyway; one nudge is plenty.
    if _recently_prompted_for_contact(
        db,
        chat_id=inbound.chat_id,
        cooldown_hours=settings.AI_TELEGRAM_CONTACT_PROMPT_COOLDOWN_HOURS,
    ):
        logger.info(
            "Telegram inbox: ignoring unknown sender id=%s @%s "
            "(update_id=%s) — already prompted within cooldown",
            inbound.from_user_id,
            inbound.from_username or "?",
            inbound.update_id,
        )
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=(
                silent_reason
                + " (Suppressed contact prompt: within "
                f"{settings.AI_TELEGRAM_CONTACT_PROMPT_COOLDOWN_HOURS}h "
                "cooldown.)"
            ),
            **common_audit,
        )

    # Send the prompt. We audit BEFORE the network call so a transient
    # send failure still gets recorded; we then patch the row status
    # if the send actually fails.
    audit = _save_audit(
        db,
        family_id=None,
        person_id=None,
        status="prompted_for_contact_share",
        status_reason=(
            "Sender unknown; prompted for one-tap phone share so Avi "
            "can auto-bind to a matching Person row."
        ),
        **common_audit,
    )

    prompt_text = (
        "Hi! I'm Avi, the family assistant. I don't recognise this "
        "Telegram account yet, so I can't reply. If you're a member of "
        "the household, tap the button below to share your phone "
        "number with me — I'll match it against the family directory "
        "and link you up. (Your number stays inside this household; "
        "Telegram only shares it after you confirm the dialog.)"
    )
    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=telegram.truncate_for_telegram(
                prompt_text, max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS
            ),
            reply_to_message_id=inbound.message_id,
            reply_markup=telegram.build_request_contact_keyboard(),
        )
    except telegram.TelegramSendError as exc:
        logger.exception(
            "Telegram inbox: contact-share prompt send failed for "
            "update_id=%s",
            inbound.update_id,
        )
        audit.status = "failed"
        audit.status_reason = (
            f"Tried to send request_contact prompt but: {exc}"
        )
        db.commit()
        return audit

    audit.reply_telegram_message_id = reply_message_id
    db.commit()
    logger.info(
        "Telegram inbox: prompted unknown sender chat=%s for contact "
        "share (update_id=%s, reply_message_id=%s)",
        inbound.chat_id,
        inbound.update_id,
        reply_message_id,
    )
    return audit


def _recently_prompted_for_contact(
    db: Session,
    *,
    chat_id: int,
    cooldown_hours: int,
) -> bool:
    """True iff this chat already received a contact-share prompt
    inside the cooldown window."""
    if cooldown_hours <= 0:
        return False
    horizon = _utcnow() - timedelta(hours=int(cooldown_hours))
    return (
        db.execute(
            select(models.TelegramInboxMessage.telegram_inbox_message_id)
            .where(
                models.TelegramInboxMessage.telegram_chat_id == chat_id,
                models.TelegramInboxMessage.status
                == "prompted_for_contact_share",
                models.TelegramInboxMessage.received_at >= horizon,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _try_link_via_shared_contact(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
) -> Optional[models.TelegramInboxMessage]:
    """Consume a ``message.contact`` payload and bind it to a Person.

    Honours the same anti-impersonation guards as the /start invite
    claim path:

    * The shared contact's ``user_id`` must equal the sender's
      ``from.id`` — otherwise the sender forwarded somebody else's
      vCard, which we refuse to act on.
    * The matched Person must not already be bound to a *different*
      Telegram identity (and the sender's Telegram id must not be
      bound to a *different* Person).
    * The phone match must be exactly ONE Person across the entire
      household DB. Zero matches → polite refusal. Multiple matches
      (e.g. shared landline) → refusal with explanation. Either way
      we never pick arbitrarily.

    Returns the audit row when this branch handled the message, or
    ``None`` to fall through to the regular person-lookup gate (e.g.
    the sender turned out to be already linked, in which case the
    contact share is just informational).
    """
    settings = get_settings()
    contact = inbound.shared_contact
    if contact is None:
        return None

    # Anti-impersonation guard: the shared contact's user_id must
    # match the sender. Telegram clients only allow sharing your own
    # contact via the ``request_contact`` button, but a malicious
    # client could PUT arbitrary JSON on the wire — assume nothing.
    if (
        contact.contact_user_id is not None
        and inbound.from_user_id is not None
        and contact.contact_user_id != inbound.from_user_id
    ):
        logger.warning(
            "Telegram inbox: refusing contact share — contact.user_id=%s "
            "!= message.from.id=%s (update_id=%s)",
            contact.contact_user_id,
            inbound.from_user_id,
            inbound.update_id,
        )
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                "Shared contact rejected: contact.user_id="
                f"{contact.contact_user_id} != "
                f"message.from.id={inbound.from_user_id}."
            ),
            user_message=(
                "I can only auto-link the phone number that belongs "
                "to the Telegram account you're chatting from. Try "
                "again and pick your own contact when Telegram asks."
            ),
        )

    if not settings.AI_TELEGRAM_AUTO_LINK_BY_PHONE:
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                "Auto-link by phone disabled "
                "(AI_TELEGRAM_AUTO_LINK_BY_PHONE=false)."
            ),
            user_message=(
                "Thanks, but I can't auto-link by phone in this "
                "household. Ask whoever set up Avi to send you a "
                "Telegram invite instead."
            ),
        )

    normalised = normalize_phone(contact.phone_number)
    if normalised is None:
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                f"Could not normalise shared phone "
                f"{contact.phone_number!r}."
            ),
            user_message=(
                "I couldn't read that phone number. Try sharing your "
                "contact again, or ask the household to send you a "
                "Telegram invite."
            ),
        )

    matches = _people_matching_phone(db, normalised)

    if not matches:
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                f"No Person row has a phone matching {normalised}."
            ),
            user_message=(
                "Thanks for sharing, but that number isn't on file "
                "for this household. Ask whoever set up Avi to add "
                "you (or send you a Telegram invite link)."
            ),
        )

    if len(matches) > 1:
        # Don't guess — multiple People share this number (e.g. a
        # household landline). The household admin needs to add a
        # distinct mobile or send an invite explicitly.
        logger.warning(
            "Telegram inbox: %d Person rows match phone %s — refusing "
            "to auto-link from update_id=%s",
            len(matches),
            normalised,
            inbound.update_id,
        )
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                f"Phone {normalised} matches {len(matches)} Person rows "
                f"({sorted(p.person_id for p in matches)}); refusing to "
                "auto-link ambiguously."
            ),
            user_message=(
                "That number is on file for more than one household "
                "member, so I can't tell which one is you. Ask the "
                "household admin to send you a Telegram invite link "
                "instead."
            ),
        )

    person = matches[0]

    # Anti-impersonation guard: the matched Person already has a
    # *different* Telegram identity bound. Refuse rather than silently
    # rotate — same rule as the /start invite claim path.
    if (
        person.telegram_user_id is not None
        and inbound.from_user_id is not None
        and person.telegram_user_id != inbound.from_user_id
    ):
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                f"Phone {normalised} matches person_id={person.person_id} "
                f"but that person is already bound to "
                f"telegram_user_id={person.telegram_user_id}; refusing "
                f"to rebind to {inbound.from_user_id}."
            ),
            user_message=(
                "That phone number is on file, but it's already linked "
                "to a different Telegram account. The household admin "
                "needs to clear the old link before I can switch."
            ),
        )

    # And the converse: the sender's Telegram id is already bound to
    # *some other* Person row. Bail rather than create a duplicate
    # binding.
    if inbound.from_user_id is not None:
        existing_owner = db.execute(
            select(models.Person)
            .where(models.Person.telegram_user_id == inbound.from_user_id)
            .where(models.Person.person_id != person.person_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing_owner is not None:
            return _send_contact_link_failure(
                db,
                inbound=inbound,
                common_audit=common_audit,
                audit_status="ignored_unknown_sender",
                audit_reason=(
                    f"telegram_user_id={inbound.from_user_id} is already "
                    f"bound to person_id={existing_owner.person_id}; "
                    f"refusing to also bind to person_id={person.person_id}."
                ),
                user_message=(
                    "Your Telegram account is already linked to a "
                    "different person on file, so I can't add another "
                    "binding. Ask the household admin to sort it out."
                ),
            )

    # ---- Initiate SMS verification (DO NOT BIND YET) ---------------
    # Phone numbers inside ``message.contact`` are supplied by the
    # sender's client and a custom MTProto build can forge any value
    # there. We never trust the contact share alone — instead we mint
    # a 6-digit code, text it to the matched Person via Twilio, and
    # only flip ``Person.telegram_user_id`` after the user echoes the
    # code back into Telegram. That proves they control BOTH the
    # Telegram account AND the household-registered phone, closing
    # the impersonation gap. See
    # ``models/telegram_contact_verification.py`` for the full
    # threat-model write-up.
    return _initiate_phone_verification(
        db,
        inbound=inbound,
        common_audit=common_audit,
        person=person,
        normalised_phone=normalised,
    )


def _people_matching_phone(
    db: Session, normalised_phone: str
) -> List[models.Person]:
    """Return every Person whose mobile/home/work phone normalises
    to ``normalised_phone``.

    We do the normalisation in Python (not SQL) because the stored
    values are messy free text typed by the household admin —
    parentheses, dashes, leading-1 with or without ``+``. Pulling
    candidates by digit-only LIKE is a cheap pre-filter that keeps
    the in-memory normalise loop tiny even on large directories.
    """
    if not normalised_phone:
        return []
    # The normalised form is always ``+<digits>``. Stripping the +
    # leaves the digit string we can use for a SUBSTRING-style
    # pre-filter — handles "(415) 555-1234" vs "+14155551234"
    # equally well because both contain "4155551234".
    last_seven = normalised_phone[-7:]
    if not last_seven:
        return []

    candidates = db.execute(
        select(models.Person).where(
            or_(
                models.Person.mobile_phone_number.ilike(f"%{last_seven}%"),
                models.Person.home_phone_number.ilike(f"%{last_seven}%"),
                models.Person.work_phone_number.ilike(f"%{last_seven}%"),
            )
        )
    ).scalars().all()

    matched: List[models.Person] = []
    for cand in candidates:
        for raw in (
            cand.mobile_phone_number,
            cand.home_phone_number,
            cand.work_phone_number,
        ):
            if raw and normalize_phone(raw) == normalised_phone:
                matched.append(cand)
                break
    return matched


def _send_contact_link_failure(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
    audit_status: str,
    audit_reason: str,
    user_message: str,
) -> models.TelegramInboxMessage:
    """Audit a contact-share rejection AND tell the sender why.

    We always reply (even though ``audit_status`` is an "ignored"
    verdict) because the user just took an explicit action — the
    polite thing is to acknowledge it instead of leaving them
    staring at a silent chat. We also clear the request-contact
    keyboard so they don't keep tapping it.
    """
    settings = get_settings()
    audit = _save_audit(
        db,
        family_id=None,
        person_id=None,
        status=audit_status,
        status_reason=audit_reason,
        **common_audit,
    )
    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=telegram.truncate_for_telegram(
                user_message,
                max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
            ),
            reply_to_message_id=inbound.message_id,
            reply_markup=telegram.remove_keyboard_markup(),
        )
        audit.reply_telegram_message_id = reply_message_id
        db.commit()
    except telegram.TelegramSendError as exc:
        # Audit row already records the verdict; surface the send
        # failure but don't promote the row to "failed" — the verdict
        # itself is still accurate (we did NOT link).
        logger.warning(
            "Telegram contact-share: failure-explanation send failed "
            "for update_id=%s: %s",
            inbound.update_id,
            exc,
        )
    return audit


# ---------------------------------------------------------------------------
# SMS-based 2FA for the contact-share auto-link flow
# ---------------------------------------------------------------------------


def _initiate_phone_verification(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
    person: models.Person,
    normalised_phone: str,
) -> models.TelegramInboxMessage:
    """Mint a code, text it via Twilio, audit the challenge.

    Called from :func:`_try_link_via_shared_contact` after every
    anti-impersonation guard has passed and we've reduced the
    contact share to exactly one matching ``Person``. We deliberately
    do NOT touch ``Person.telegram_user_id`` here — the bind only
    happens once the user proves SMS control by echoing the code
    back inside :func:`_handle_verification_attempt`.

    Idempotency: if there's an in-flight (unclaimed, unrevoked)
    challenge for this chat already we revoke it before inserting a
    new row, both because the partial unique index would otherwise
    refuse the INSERT and because the user is clearly restarting.
    """
    settings = get_settings()

    # Check Twilio is wired up before we mint anything; otherwise we'd
    # leave a row in the table that can never be satisfied because no
    # SMS will ever arrive.
    if (
        not settings.TWILIO_ACCOUNT_SID
        or not settings.TWILIO_AUTH_TOKEN
        or not settings.TWILIO_PRIMARY_PHONE
    ):
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="failed",
            audit_reason=(
                "Cannot start SMS verification: Twilio credentials "
                "(TWILIO_ACCOUNT_SID/AUTH_TOKEN/PRIMARY_PHONE) are not "
                "fully configured."
            ),
            user_message=(
                "I can't text you a verification code right now — the "
                "household admin needs to finish setting up SMS. Ask "
                "them for a Telegram invite link instead."
            ),
        )

    # Revoke any in-flight verification for this chat (and, defensively,
    # for this telegram_user_id) so the partial unique index is happy
    # and we don't leave dangling challenges around if the user is
    # re-sharing contact mid-flow.
    _revoke_outstanding_verifications(
        db,
        chat_id=inbound.chat_id,
        telegram_user_id=inbound.from_user_id,
    )

    code = models.generate_verification_code(
        length=settings.AI_TELEGRAM_VERIFY_CODE_LENGTH
    )
    code_hash = models.hash_verification_code(code)
    expires_at = _utcnow() + timedelta(
        minutes=int(settings.AI_TELEGRAM_VERIFY_TTL_MINUTES)
    )

    verification = models.TelegramContactVerification(
        family_id=person.family_id,
        person_id=person.person_id,
        telegram_user_id=int(inbound.from_user_id or 0),
        telegram_username=inbound.from_username,
        telegram_chat_id=int(inbound.chat_id),
        phone_normalised=normalised_phone,
        code_hash=code_hash,
        max_attempts=int(settings.AI_TELEGRAM_VERIFY_MAX_ATTEMPTS),
        expires_at=expires_at,
    )
    db.add(verification)
    db.flush()

    # ---- Send the code over SMS ------------------------------------
    sms_body = (
        f"Avi here. Your verification code is {code}. Reply with this "
        "code in our Telegram chat to finish linking your account. "
        f"Expires in {int(settings.AI_TELEGRAM_VERIFY_TTL_MINUTES)} "
        "minutes."
    )
    try:
        sms_sid = twilio_sms.send_sms(
            account_sid=settings.TWILIO_ACCOUNT_SID or "",
            auth_token=settings.TWILIO_AUTH_TOKEN or "",
            from_phone=settings.TWILIO_PRIMARY_PHONE,
            to_phone=normalised_phone,
            body=sms_body,
        )
    except twilio_sms.TwilioSendError as exc:
        # Couldn't text — revoke the row so it can't be exploited and
        # tell the user to try again or ask for an invite. Audit
        # captures the underlying Twilio error.
        verification.revoked_at = _utcnow()
        db.commit()
        logger.exception(
            "Telegram contact-share: Twilio send_sms failed for "
            "chat_id=%s person_id=%s phone=%s",
            inbound.chat_id,
            person.person_id,
            normalised_phone,
        )
        return _send_contact_link_failure(
            db,
            inbound=inbound,
            common_audit=common_audit,
            audit_status="failed",
            audit_reason=(
                f"Twilio send_sms failed for verification code: {exc}"
            ),
            user_message=(
                "I matched your number to the household directory but "
                "couldn't text you the verification code. Try sharing "
                "your contact again in a few minutes, or ask the "
                "household admin for a Telegram invite link."
            ),
        )

    verification.twilio_message_sid = sms_sid
    db.flush()

    # ---- Open / reuse a session so the prompt lands in history ----
    session, _created = live_session.find_or_create_telegram_session(
        db,
        family_id=person.family_id,
        chat_id=inbound.chat_id,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)

    audit = _save_audit(
        db,
        family_id=person.family_id,
        person_id=person.person_id,
        status="failed",
        status_reason=(
            "Verification-initiate pipeline started but did not "
            "complete."
        ),
        live_session_id=session.live_session_id,
        **common_audit,
    )

    masked = _mask_phone_for_display(normalised_phone)
    prompt = (
        f"Thanks. I texted a {settings.AI_TELEGRAM_VERIFY_CODE_LENGTH}-"
        f"digit code to {masked}. Paste it here to finish linking. "
        f"It expires in {int(settings.AI_TELEGRAM_VERIFY_TTL_MINUTES)} "
        "minutes; you have "
        f"{int(settings.AI_TELEGRAM_VERIFY_MAX_ATTEMPTS)} tries."
    )
    prompt = telegram.truncate_for_telegram(
        prompt, max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS
    )

    live_session.log_message(
        db,
        session,
        role="user",
        content=(
            f"From: {inbound.sender_display_name or '@' + (inbound.from_username or '?')}\n"
            f"Chat: {inbound.chat_id}\n"
            "\n[Shared contact via Telegram request_contact button]\n"
            f"phone={normalised_phone}"
        ),
        person_id=person.person_id,
        meta={
            "kind": "telegram_contact_share",
            "telegram_update_id": inbound.update_id,
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": inbound.message_id,
            "from_user_id": inbound.from_user_id,
            "from_username": inbound.from_username,
            "phone_normalised": normalised_phone,
        },
    )

    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=prompt,
            reply_to_message_id=inbound.message_id,
            # Clear the "Share my phone number" keyboard — the next
            # action is "type the code", not "tap a button".
            reply_markup=telegram.remove_keyboard_markup(),
        )
    except telegram.TelegramSendError as exc:
        logger.exception(
            "Telegram contact-share: verification-prompt send failed "
            "for update_id=%s",
            inbound.update_id,
        )
        audit.status = "failed"
        audit.status_reason = f"send_message: {exc}"
        db.commit()
        return audit

    live_session.log_message(
        db,
        session,
        role="assistant",
        content=prompt,
        person_id=person.person_id,
        meta={
            "kind": "telegram_contact_share_verify_prompt",
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": reply_message_id,
            "in_reply_to": inbound.message_id,
            "telegram_contact_verification_id": (
                verification.telegram_contact_verification_id
            ),
            "twilio_message_sid": sms_sid,
        },
    )
    audit.status = "processed_replied"
    audit.status_reason = (
        f"Initiated SMS verification: verification_id="
        f"{verification.telegram_contact_verification_id}, "
        f"texted code to {masked} (sid={sms_sid})."
    )
    audit.reply_telegram_message_id = reply_message_id
    db.commit()

    logger.info(
        "Telegram contact-share: verification initiated person_id=%s "
        "family_id=%s telegram_user_id=%s phone=%s sid=%s "
        "(update_id=%s)",
        person.person_id,
        person.family_id,
        inbound.from_user_id,
        normalised_phone,
        sms_sid,
        inbound.update_id,
    )
    return audit


def _pending_verification_for_chat(
    db: Session,
    *,
    chat_id: int,
    telegram_user_id: Optional[int],
) -> Optional[models.TelegramContactVerification]:
    """Return the outstanding (unclaimed, unrevoked, in-window) row
    for ``chat_id`` if any.

    We require the inbound's ``from.id`` to match the row's
    ``telegram_user_id`` so a stranger who somehow got into the same
    chat can't consume the legitimate user's code. (In private chats
    this is always the same user, but defence-in-depth is cheap.)
    """
    row = db.execute(
        select(models.TelegramContactVerification).where(
            models.TelegramContactVerification.telegram_chat_id == chat_id,
            models.TelegramContactVerification.claimed_at.is_(None),
            models.TelegramContactVerification.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if (
        telegram_user_id is not None
        and row.telegram_user_id != int(telegram_user_id)
    ):
        # Different Telegram user pasting in the same chat — ignore
        # silently. The legitimate user's challenge stays valid.
        return None
    return row


def _revoke_outstanding_verifications(
    db: Session,
    *,
    chat_id: int,
    telegram_user_id: Optional[int],
) -> None:
    """Mark every in-flight challenge for ``chat_id`` revoked.

    Defensive: we also sweep any row matching ``telegram_user_id``
    in case the user is mid-flow in a different chat (e.g. they
    re-shared contact from a brand-new chat). The partial unique
    index only prevents two rows for the same chat — orphans across
    chats would not violate it but would still confuse the next
    attempt.
    """
    candidates = db.execute(
        select(models.TelegramContactVerification).where(
            models.TelegramContactVerification.claimed_at.is_(None),
            models.TelegramContactVerification.revoked_at.is_(None),
        ).where(
            or_(
                models.TelegramContactVerification.telegram_chat_id
                == chat_id,
                (
                    models.TelegramContactVerification.telegram_user_id
                    == int(telegram_user_id)
                )
                if telegram_user_id is not None
                else models.TelegramContactVerification.telegram_chat_id
                == chat_id,  # no-op duplicate so the OR is well-formed
            )
        )
    ).scalars().all()
    now = _utcnow()
    for row in candidates:
        row.revoked_at = now
    if candidates:
        db.flush()


def _handle_verification_attempt(
    db: Session,
    *,
    verification: models.TelegramContactVerification,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
) -> models.TelegramInboxMessage:
    """Treat ``inbound`` as a possible answer to an outstanding code.

    Three branches:

    * **Expired** — politely tell the user to restart and revoke the
      row so a future contact-share can mint a fresh challenge.
    * **No code-shaped digits in the message** — remind the user we're
      waiting for a code; do not consume an attempt.
    * **Code-shaped attempt** — compare against the stored hash. On
      match: bind ``Person.telegram_user_id`` and send a welcome.
      On mismatch: increment ``attempts``; if the budget is exhausted,
      revoke and ask the user to restart.
    """
    settings = get_settings()
    now = _utcnow()

    # ---- Expired before they got around to replying ----------------
    if verification.expires_at <= now:
        verification.revoked_at = now
        db.flush()
        return _send_verification_status_reply(
            db,
            inbound=inbound,
            common_audit=common_audit,
            verification=verification,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                "Verification challenge expired without being claimed."
            ),
            user_message=(
                "That verification code expired. Tap the button below "
                "to share your contact and I'll text you a fresh one."
            ),
            attach_share_keyboard=True,
        )

    code_attempt = _extract_verification_code_attempt(
        inbound.body,
        expected_length=int(settings.AI_TELEGRAM_VERIFY_CODE_LENGTH),
    )

    # ---- Free-form chitchat while waiting for the code -------------
    if code_attempt is None:
        return _send_verification_status_reply(
            db,
            inbound=inbound,
            common_audit=common_audit,
            verification=verification,
            audit_status="processed_replied",
            audit_reason=(
                f"User sent non-code text while verification "
                f"{verification.telegram_contact_verification_id} "
                "is pending."
            ),
            user_message=(
                "I'm still waiting for the "
                f"{int(settings.AI_TELEGRAM_VERIFY_CODE_LENGTH)}-digit "
                "code I just texted you. Paste it here to finish "
                "linking, or tap below to share your contact again "
                "if you didn't get it."
            ),
            attach_share_keyboard=True,
        )

    # ---- Correct code -> bind --------------------------------------
    if models.verification_codes_match(
        expected_hash=verification.code_hash,
        provided_code=code_attempt,
    ):
        return _complete_verification_binding(
            db,
            verification=verification,
            inbound=inbound,
            common_audit=common_audit,
        )

    # ---- Wrong code -> spend an attempt ----------------------------
    verification.attempts += 1
    db.flush()

    remaining = verification.attempts_remaining()
    if remaining <= 0:
        verification.revoked_at = now
        db.flush()
        return _send_verification_status_reply(
            db,
            inbound=inbound,
            common_audit=common_audit,
            verification=verification,
            audit_status="ignored_unknown_sender",
            audit_reason=(
                f"Verification {verification.telegram_contact_verification_id} "
                "exhausted attempt budget; revoked."
            ),
            user_message=(
                "Too many wrong codes — I've cancelled this attempt. "
                "Tap the button below to share your contact again and "
                "I'll text you a fresh code."
            ),
            attach_share_keyboard=True,
        )

    return _send_verification_status_reply(
        db,
        inbound=inbound,
        common_audit=common_audit,
        verification=verification,
        audit_status="processed_replied",
        audit_reason=(
            f"Verification {verification.telegram_contact_verification_id}: "
            f"wrong code (attempt {verification.attempts}/"
            f"{verification.max_attempts})."
        ),
        user_message=(
            f"That code didn't match. Try again — you have "
            f"{remaining} "
            f"{'try' if remaining == 1 else 'tries'} left."
        ),
        attach_share_keyboard=False,
    )


def _complete_verification_binding(
    db: Session,
    *,
    verification: models.TelegramContactVerification,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
) -> models.TelegramInboxMessage:
    """User passed verification: do the actual Person bind + welcome."""
    settings = get_settings()

    person = db.get(models.Person, verification.person_id)
    if person is None:
        # Person was deleted between challenge issuance and code reply.
        verification.revoked_at = _utcnow()
        db.flush()
        return _send_verification_status_reply(
            db,
            inbound=inbound,
            common_audit=common_audit,
            verification=verification,
            audit_status="failed",
            audit_reason=(
                f"Verification {verification.telegram_contact_verification_id} "
                f"target person_id={verification.person_id} no longer "
                "exists; cannot bind."
            ),
            user_message=(
                "Something changed on the household side while I was "
                "waiting for your code. Ask the admin to send you a "
                "Telegram invite link."
            ),
            attach_share_keyboard=False,
        )

    # Re-run the "telegram_user_id already bound to someone else"
    # guard one last time. The state could have changed between
    # challenge issuance and code reply (e.g. the household admin
    # bound the same Telegram id to a different Person via the UI).
    if inbound.from_user_id is not None:
        existing_owner = db.execute(
            select(models.Person)
            .where(models.Person.telegram_user_id == inbound.from_user_id)
            .where(models.Person.person_id != person.person_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing_owner is not None:
            verification.revoked_at = _utcnow()
            db.flush()
            return _send_verification_status_reply(
                db,
                inbound=inbound,
                common_audit=common_audit,
                verification=verification,
                audit_status="ignored_unknown_sender",
                audit_reason=(
                    f"Verification ok but telegram_user_id="
                    f"{inbound.from_user_id} is now bound to "
                    f"person_id={existing_owner.person_id}; refusing "
                    f"to also bind to person_id={person.person_id}."
                ),
                user_message=(
                    "Your Telegram account is now linked to a "
                    "different person on file, so I can't add another "
                    "binding. Ask the household admin to sort it out."
                ),
                attach_share_keyboard=False,
            )

    # ---- Apply the binding -----------------------------------------
    person.telegram_user_id = inbound.from_user_id
    if inbound.from_username and not person.telegram_username:
        person.telegram_username = inbound.from_username
    verification.claimed_at = _utcnow()
    db.flush()

    session, _created = live_session.find_or_create_telegram_session(
        db,
        family_id=person.family_id,
        chat_id=inbound.chat_id,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)

    audit = _save_audit(
        db,
        family_id=person.family_id,
        person_id=person.person_id,
        status="failed",
        status_reason=(
            "Verification-claim pipeline started but did not complete."
        ),
        live_session_id=session.live_session_id,
        **common_audit,
    )

    family = db.get(models.Family, person.family_id)
    assistant_name = (
        family.assistant.assistant_name
        if family and family.assistant
        else "Avi"
    )
    welcome = telegram.truncate_for_telegram(
        _build_invite_welcome(
            assistant_name=assistant_name, person=person
        ),
        max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
    )

    live_session.log_message(
        db,
        session,
        role="user",
        content=_format_inbound_for_log(inbound, []),
        person_id=person.person_id,
        meta={
            "kind": "telegram_contact_share_verify_code",
            "telegram_update_id": inbound.update_id,
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": inbound.message_id,
            "from_user_id": inbound.from_user_id,
            "from_username": inbound.from_username,
            "telegram_contact_verification_id": (
                verification.telegram_contact_verification_id
            ),
        },
    )

    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=welcome,
            reply_to_message_id=inbound.message_id,
            reply_markup=telegram.remove_keyboard_markup(),
        )
    except telegram.TelegramSendError as exc:
        logger.exception(
            "Telegram verification-claim: welcome send failed for "
            "update_id=%s",
            inbound.update_id,
        )
        audit.status = "failed"
        audit.status_reason = f"send_message: {exc}"
        db.commit()
        return audit

    live_session.log_message(
        db,
        session,
        role="assistant",
        content=welcome,
        person_id=person.person_id,
        meta={
            "kind": "telegram_contact_share_welcome",
            "telegram_chat_id": inbound.chat_id,
            "telegram_message_id": reply_message_id,
            "in_reply_to": inbound.message_id,
            "telegram_contact_verification_id": (
                verification.telegram_contact_verification_id
            ),
        },
    )
    audit.status = "processed_replied"
    audit.status_reason = (
        f"Verified contact share: claimed verification_id="
        f"{verification.telegram_contact_verification_id}, bound "
        f"person_id={person.person_id} to telegram_user_id="
        f"{inbound.from_user_id}."
    )
    audit.reply_telegram_message_id = reply_message_id
    db.commit()

    logger.info(
        "Telegram contact-share: verification claimed person_id=%s "
        "family_id=%s telegram_user_id=%s @%s verification_id=%s "
        "(update_id=%s)",
        person.person_id,
        person.family_id,
        inbound.from_user_id,
        inbound.from_username or "?",
        verification.telegram_contact_verification_id,
        inbound.update_id,
    )
    return audit


def _send_verification_status_reply(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    common_audit: dict,
    verification: models.TelegramContactVerification,
    audit_status: str,
    audit_reason: str,
    user_message: str,
    attach_share_keyboard: bool,
) -> models.TelegramInboxMessage:
    """Audit the outcome of a verification interaction + reply once.

    Used by every non-success verification branch (expired, wrong
    code, free-form chitchat, exhausted budget) so they share one
    audit + reply path. ``attach_share_keyboard`` controls whether
    we re-pop the request_contact button — only useful when the
    caller wants the user to restart from scratch.
    """
    settings = get_settings()
    audit = _save_audit(
        db,
        family_id=verification.family_id,
        person_id=verification.person_id,
        status=audit_status,
        status_reason=audit_reason,
        **common_audit,
    )
    reply_markup: Optional[dict] = None
    if attach_share_keyboard:
        reply_markup = telegram.build_request_contact_keyboard()
    else:
        reply_markup = telegram.remove_keyboard_markup()
    try:
        reply_message_id = telegram.send_message(
            bot_token=settings.TELEGRAM_BOT_TOKEN or "",
            chat_id=inbound.chat_id,
            text=telegram.truncate_for_telegram(
                user_message,
                max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
            ),
            reply_to_message_id=inbound.message_id,
            reply_markup=reply_markup,
        )
        audit.reply_telegram_message_id = reply_message_id
        db.commit()
    except telegram.TelegramSendError as exc:
        logger.warning(
            "Telegram verification: status reply send failed for "
            "update_id=%s: %s",
            inbound.update_id,
            exc,
        )
        db.commit()
    return audit


def _extract_verification_code_attempt(
    body: Optional[str], *, expected_length: int
) -> Optional[str]:
    """Return a normalised code if ``body`` looks like a code attempt.

    We're permissive about formatting (the user might paste
    ``123 456`` or ``Code: 123456``) but conservative about
    length — only an exact ``expected_length`` digit count counts as
    an attempt. That way a chatty "I have 2 questions about my 3 kids"
    isn't accidentally treated as a 6-digit pile of digits and burned
    against the attempt budget.
    """
    if not body:
        return None
    digits = "".join(ch for ch in body if ch.isdigit())
    if len(digits) != expected_length:
        return None
    # Bound the input length too — a 100-character message that
    # happens to contain exactly 6 digits scattered through it is
    # almost certainly not a code paste. Keep the gate tight.
    if len(body.strip()) > expected_length + 12:
        return None
    return digits


def _mask_phone_for_display(phone: str) -> str:
    """Hide all but the last 4 digits of an E.164 phone number.

    Used in the "I texted XXX" reply to confirm we routed the code to
    the number the user expects without re-disclosing the household's
    full directory entry in the chat.
    """
    if not phone:
        return "your phone"
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return phone
    return f"+{'*' * (len(digits) - 4)}{digits[-4:]}"


def _already_processed(db: Session, telegram_update_id: int) -> bool:
    return (
        db.execute(
            select(models.TelegramInboxMessage.telegram_inbox_message_id)
            .where(
                models.TelegramInboxMessage.telegram_update_id
                == telegram_update_id
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _lookup_family_member_by_telegram(
    db: Session,
    *,
    user_id: Optional[int],
    username: Optional[str],
) -> Optional[models.Person]:
    """Find the unique person whose Telegram identity matches.

    Prefers ``telegram_user_id`` (stable, never re-assigned) and falls
    back to ``telegram_username`` (case-insensitive, mutable). If both
    are present we OR them in one query so a person who changed their
    @handle but kept the same id still resolves cleanly.
    """
    if user_id is None and not username:
        return None

    clauses = []
    if user_id is not None:
        clauses.append(models.Person.telegram_user_id == user_id)
    if username:
        clauses.append(
            models.Person.telegram_username.ilike(username)
        )

    return db.execute(
        select(models.Person).where(or_(*clauses)).limit(1)
    ).scalar_one_or_none()


def _save_audit(db: Session, **kwargs) -> models.TelegramInboxMessage:
    """Insert (or, on dedup, fetch) the telegram_inbox_messages row."""
    row = models.TelegramInboxMessage(**kwargs)
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(models.TelegramInboxMessage)
            .where(
                models.TelegramInboxMessage.telegram_update_id
                == kwargs["telegram_update_id"]
            )
            .limit(1)
        ).scalar_one()
        return existing


def _persist_attachments(
    db: Session,
    audit: models.TelegramInboxMessage,
    inbound: telegram.InboundTelegramMessage,
) -> List[dict]:
    """Download every Telegram media file and write attachment rows.

    Returns a list of ``{media_index, kind, mime_type, stored_path,
    file_size_bytes}`` dicts so the caller can mention them in the
    transcript meta. Failures on one media file don't block the
    others — we log and keep going so a transient Bot API hiccup
    doesn't drop the whole reply.
    """
    if not inbound.attachments or audit.family_id is None:
        return []
    settings = get_settings()
    bot_token = settings.TELEGRAM_BOT_TOKEN or ""
    out: List[dict] = []
    for ref in inbound.attachments:
        try:
            blob = telegram.download_file(
                bot_token=bot_token,
                file_id=ref.file_id,
                hint_mime=ref.mime_type,
            )
        except telegram.TelegramReadError as exc:
            logger.warning(
                "Telegram inbox: failed to fetch %s file_id=%s for update_id=%s: %s",
                ref.kind,
                ref.file_id,
                inbound.update_id,
                exc,
            )
            continue
        rel_path, size = storage.save_telegram_attachment(
            family_id=audit.family_id,
            telegram_inbox_message_id=audit.telegram_inbox_message_id,
            file_bytes=blob.file_bytes,
            extension=telegram.extension_for_mime(blob.mime_type),
        )
        att = models.TelegramInboxAttachment(
            telegram_inbox_message_id=audit.telegram_inbox_message_id,
            media_index=ref.media_index,
            kind=ref.kind,
            telegram_file_id=ref.file_id,
            mime_type=blob.mime_type,
            file_size_bytes=size,
            stored_path=rel_path,
        )
        db.add(att)
        out.append(
            {
                "media_index": ref.media_index,
                "kind": ref.kind,
                "mime_type": blob.mime_type,
                "stored_path": rel_path,
                "file_size_bytes": size,
            }
        )
    if out:
        db.flush()
    return out


def _format_inbound_for_log(
    inbound: telegram.InboundTelegramMessage,
    attachments: List[dict],
) -> str:
    """Render an inbound Telegram message so it reads naturally in history."""
    sender_label = (
        inbound.sender_display_name
        or (f"@{inbound.from_username}" if inbound.from_username else None)
        or (str(inbound.from_user_id) if inbound.from_user_id else "(unknown)")
    )
    lines = [
        f"From: {sender_label}",
        f"Chat: {inbound.chat_id}",
        "",
        inbound.body or "(no text)",
    ]
    if attachments:
        lines.append("")
        for att in attachments:
            lines.append(
                f"[attachment {att['media_index']}: {att['kind']} "
                f"({att['mime_type']}, {att['file_size_bytes']} bytes)]"
            )
    return "\n".join(lines)


def _format_user_message_for_agent(
    inbound: telegram.InboundTelegramMessage, person: models.Person
) -> str:
    """Wrap the Telegram message so the agent knows the surface."""
    name = (
        person.preferred_name
        or person.first_name
        or inbound.sender_display_name
        or (f"@{inbound.from_username}" if inbound.from_username else "?")
    )
    handle_hint = f" @{inbound.from_username}" if inbound.from_username else ""
    media_hint = ""
    if inbound.num_media:
        kinds = ", ".join(a.kind for a in inbound.attachments)
        media_hint = f" (with {inbound.num_media} attached: {kinds})"
    return (
        f"[Telegram message from {name}{handle_hint}{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no text)'}"
    )


def _build_telegram_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    sender_display_name: str,
) -> str:
    """Mirror the SMS system prompt, but tuned for a Telegram reply.

    Differs from the SMS build:
    * Reply may be longer (Telegram has no per-segment carrier cost)
      but should still feel like a chat message, not an essay.
    * Lightweight Markdown is fine in principle but we tell the agent
      to stick to plain text because we deliberately omit
      ``parse_mode`` on send to avoid hard-failing on stray ``*``.
    """
    family = db.get(models.Family, family_id)
    assistant_name = (
        family.assistant.assistant_name if family and family.assistant else "Avi"
    )
    family_name = family.family_name if family else None

    rag_block = ""
    if family is not None:
        rag_block = rag.build_family_overview(
            db, family, requestor_person_id=person.person_id
        )
    person_block = "Currently chatting with on Telegram:\n" + rag.build_person_context(
        db, person, requestor_person_id=person.person_id
    )

    registry = agent_tools.build_default_registry()
    capabilities = agent_tools.detect_capabilities(
        db, _assistant_id_for_family(db, family_id)
    )
    capabilities_block = agent_tools.describe_capabilities(registry, capabilities)

    parts = [
        ollama.system_prompt_for_avi(assistant_name, family_name),
        "--- What you can do ---\n" + capabilities_block,
    ]
    house_context = prompts.render_context_blocks()
    if house_context:
        parts.append("--- House context ---\n" + house_context)
    parts.append(
        authz.render_speaker_scope_block(
            authz.build_speaker_scope(db, speaker_person_id=person.person_id)
        )
    )
    if rag_block:
        parts.append("--- Known household context ---\n" + rag_block)
    parts.append(person_block)
    parts.append(
        "--- Database schema you can query ---\n"
        "You have read-only access to the family Postgres database. "
        "Sensitive columns are encrypted; use the *_last_four helpers.\n\n"
        + schema_catalog.dump_text(db)
    )
    max_chars = get_settings().AI_TELEGRAM_REPLY_MAX_CHARS
    parts.append(
        "--- How to reply to this Telegram message ---\n"
        f"This message arrived via Telegram from {sender_display_name}. "
        "Your final answer will be sent verbatim as a Telegram reply "
        "(threaded under the inbound). Therefore:\n"
        f"* Keep the reply UNDER {max_chars} characters. Telegram allows "
        "  up to 4096 but most users prefer concise chat-style replies.\n"
        "* Plain text only. No Markdown, no asterisks, no underscores. "
        "  We deliberately do NOT enable Telegram's parse_mode so any "
        "  markup characters would land verbatim instead of styling.\n"
        "* No subject line, no greeting, no formal sign-off. The "
        "  recipient knows who you are; jump straight to the answer.\n"
        "* Use at most one round of tool calls before writing the "
        "  reply. Telegram is asynchronous — if you need more info, "
        "  ask the user one specific question and stop.\n"
        "* NEVER include encrypted identifiers, passwords, or anything "
        "  ending in _encrypted in the reply body.\n"
    )
    return prompts.with_safety("\n\n".join(parts))


def _assistant_id_for_family(db: Session, family_id: int) -> Optional[int]:
    """Best-effort lookup of the assistant row tied to ``family_id``."""
    row = db.execute(
        select(models.Assistant.assistant_id)
        .where(models.Assistant.family_id == family_id)
        .limit(1)
    ).scalar_one_or_none()
    return row


def _run_agent_with_fast_ack(
    db: Session,
    *,
    inbound: telegram.InboundTelegramMessage,
    person: models.Person,
    task_id: int,
    assistant_id: Optional[int],
    system_prompt: str,
    user_message: str,
    session: models.LiveSession,
    audit: models.TelegramInboxMessage,
) -> Tuple[str, bool, Optional[int]]:
    """Race the heavy agent against the fast-ack threshold.

    Behaviour:

    1. Submits the heavy agent (``_run_agent_to_completion``) to the
       shared :mod:`api.services.background_agent` thread pool so this
       caller can race a watchdog against it.
    2. Waits up to :setting:`AI_FAST_ACK_AFTER_SECONDS`.
       * If the heavy agent finished in time, returns its text — no
         ack was needed and none was sent.
       * Otherwise, asks the lightweight model for a one-sentence
         contextual ack and sends it as a Telegram reply (threaded
         under the inbound), then blocks for the heavy agent to
         finish and returns its text.
    3. Either way, returns ``(final_text, agent_failed, ack_message_id)``.
       ``ack_message_id`` is ``None`` when no ack was sent (either the
       heavy agent won the race or the fast model declined to produce
       one); otherwise it's the Telegram ``message_id`` of the ack so
       the caller can cross-link it in the transcript.

    Failure isolation matches the original blocking flow: if the
    heavy agent crashes we return a fixed apology string and
    ``agent_failed=True`` so the caller's audit row reflects it. The
    ack send is best-effort — a Telegram outage during the ack does
    not abort the heavy reply.
    """
    settings = get_settings()
    family_id = person.family_id
    person_id = person.person_id

    # ---- Kick off the heavy agent on a background worker -----------
    def _heavy() -> str:
        return _run_agent_to_completion(
            task_id=task_id,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=person_id,
            system_prompt=system_prompt,
            history=[],
            user_message=user_message,
        )

    future = background_agent.submit(_heavy)

    # ---- Race ------------------------------------------------------
    threshold = float(settings.AI_FAST_ACK_AFTER_SECONDS)
    ack_message_id: Optional[int] = None
    try:
        final_text = future.result(timeout=threshold)
        # Heavy agent won the race — skip the ack entirely. Nothing
        # to log here; the transcript will record only the final
        # assistant reply.
        return final_text, False, None
    except FuturesTimeoutError:
        pass
    except Exception:  # noqa: BLE001 - heavy agent crashed
        logger.exception(
            "Telegram inbox: agent loop crashed for update_id=%s task=%s",
            inbound.update_id,
            task_id,
        )
        return (
            "Got your message — Avi here. I hit a snag on my end and "
            "couldn't finish that just now. I'll look into it."
        ), True, None

    # ---- Heavy agent didn't finish in time — send a contextual ack -
    if settings.AI_FAST_ACK_ENABLED:
        ack_text = fast_ack.generate_contextual_ack_sync(
            surface="telegram",
            sender_display_name=(
                inbound.sender_display_name
                or person.preferred_name
                or person.first_name
            ),
            last_user_message=inbound.body or "",
        )
        if ack_text:
            ack_text = telegram.truncate_for_telegram(
                ack_text,
                max_chars=settings.AI_TELEGRAM_REPLY_MAX_CHARS,
            )
            try:
                ack_message_id = telegram.send_message(
                    bot_token=settings.TELEGRAM_BOT_TOKEN or "",
                    chat_id=inbound.chat_id,
                    text=ack_text,
                    reply_to_message_id=inbound.message_id,
                )
                logger.info(
                    "Telegram inbox: fast-ack sent chat=%s msg=%s "
                    "(update_id=%s, threshold=%.1fs)",
                    inbound.chat_id,
                    ack_message_id,
                    inbound.update_id,
                    threshold,
                )
                live_session.log_message(
                    db,
                    session,
                    role="assistant",
                    content=ack_text,
                    person_id=person_id,
                    meta={
                        "kind": "telegram_fast_ack",
                        "agent_task_id": task_id,
                        "telegram_chat_id": inbound.chat_id,
                        "telegram_message_id": ack_message_id,
                        "in_reply_to": inbound.message_id,
                    },
                )
                # Stamp the audit row mid-flight so an operator who
                # peeks before the heavy agent finishes can see we
                # already replied with an ack.
                audit.status_reason = (
                    f"Fast-ack sent (msg={ack_message_id}); awaiting "
                    "heavy agent."
                )
                db.commit()
            except telegram.TelegramSendError as exc:
                # Don't let an ack-send failure abort the heavy reply.
                # The user will still get the final answer; we just
                # missed the latency-hider.
                logger.warning(
                    "Telegram inbox: fast-ack send failed for "
                    "update_id=%s (%s) — continuing without ack",
                    inbound.update_id,
                    exc,
                )
                ack_message_id = None

    # ---- Block until the heavy agent finishes ----------------------
    try:
        final_text = future.result()
    except Exception:  # noqa: BLE001 - heavy agent crashed post-ack
        logger.exception(
            "Telegram inbox: agent loop crashed post-ack for "
            "update_id=%s task=%s",
            inbound.update_id,
            task_id,
        )
        return (
            "Got your message — Avi here. I hit a snag on my end and "
            "couldn't finish that just now. I'll look into it."
        ), True, ack_message_id

    return final_text, False, ack_message_id


def _run_agent_to_completion(
    *,
    task_id: int,
    family_id: int,
    assistant_id: Optional[int],
    person_id: int,
    system_prompt: str,
    history: List[dict],
    user_message: str,
) -> str:
    """Drain the async agent generator and return the final reply text.

    Mirrors ``services.sms_inbox._run_agent_to_completion`` /
    ``services.email_inbox._run_agent_to_completion`` exactly so all
    three messaging surfaces share behaviour.
    """
    registry = agent_tools.build_default_registry()
    with SessionLocal() as db_for_caps:
        capabilities = agent_tools.detect_capabilities(db_for_caps, assistant_id)

    final_text = ""
    error_text: Optional[str] = None

    async def _drain() -> None:
        nonlocal final_text, error_text
        async for event in agent_loop.run_agent(
            task_id=task_id,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=person_id,
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
            registry=registry,
            capabilities=capabilities,
        ):
            if event.type == "task_completed":
                final_text = (event.payload.get("summary") or "").strip()
            elif event.type == "task_failed":
                error_text = str(event.payload.get("error") or "Agent failed.")

    try:
        asyncio.run(_drain())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drain())
        finally:
            loop.close()

    if error_text and not final_text:
        return (
            "Sorry — Avi here. I tried but my server hit a snag. "
            "Try again in a bit."
        )
    return final_text or "Got your message — nothing to add right now."


__all__ = [
    "process_inbound_update",
    "run_telegram_inbox_loop",
]
