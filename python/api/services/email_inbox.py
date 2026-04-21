"""Email-driven AI assistant — inbox poller + per-message agent dispatch.

Big picture
-----------
This module turns Avi into an **email auto-responder for registered
family members only**.

* A long-lived asyncio loop (:func:`run_email_inbox_loop`) wakes every
  ``AI_EMAIL_INBOX_POLL_SECONDS`` and, for each assistant that has
  connected a Google account with the ``gmail.modify`` scope, calls
  :func:`process_assistant_inbox`.
* The per-assistant tick lists unread inbox messages, looks up each
  sender by email against ``people.email_address`` for that
  assistant's family, and only proceeds when there is a match. **All
  unmatched senders are silently ignored, recorded in
  ``email_inbox_messages`` with ``status='ignored_unknown_sender'``,
  and marked read in Gmail so we don't keep reprocessing them.** This
  is the single security gate — no other code path replies to email.
* When a sender does match a registered person we open / reuse a
  ``LiveSession`` keyed on the Gmail thread id, log the inbound
  message into the transcript, run the same agent loop the live chat
  uses, and send the final answer back as a threaded Gmail reply.
* The reply is also logged into the transcript and the
  ``email_inbox_messages`` audit row is finalised with
  ``status='processed_replied'`` (or ``'failed'`` if anything in the
  pipeline blew up).

What this code DELIBERATELY does not do
---------------------------------------
* Reply to anyone whose ``From:`` address does not exactly match (case
  insensitive, after RFC-2822 ``parseaddr`` normalisation) one
  ``Person.email_address`` for the assistant's family.
* Reply to mailing-list / auto-reply / bulk traffic. Such messages are
  recorded with ``status='ignored_bulk'`` and marked read.
* Loop on its own outbound mail (the All Mail label can show our own
  Sent items as Inbox under some configurations). Anything whose
  ``From:`` matches the assistant's connected Gmail address is
  recorded as ``ignored_self`` and marked read.

The loop catches every per-tick exception and continues so one
misbehaving assistant or one malformed message can never stop the
poller for the rest of the family.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..ai import agent as agent_loop
from ..ai import authz
from ..ai import ollama, prompts, rag, schema_catalog
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..ai import web_search_shortcut
from ..config import get_settings
from ..db import SessionLocal
from ..integrations import gmail
from ..integrations import google_oauth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_email_inbox_loop(stop_event: asyncio.Event) -> None:
    """Forever-running poller. Cancel with ``stop_event.set()``.

    Sleeps in small increments rather than one big ``asyncio.sleep`` so
    a graceful shutdown takes at most ~1 s, not the full poll interval.
    """
    settings = get_settings()
    interval = max(15, int(settings.AI_EMAIL_INBOX_POLL_SECONDS))
    logger.info(
        "Email inbox poller starting (interval=%ds, max_per_tick=%d)",
        interval,
        settings.AI_EMAIL_INBOX_MAX_PER_TICK,
    )

    while not stop_event.is_set():
        tick_started = time.monotonic()
        try:
            await _run_one_tick()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Email inbox tick crashed; continuing")

        # Sleep the remainder of this interval, but check the stop
        # event every second so shutdown is responsive.
        elapsed = time.monotonic() - tick_started
        remaining = max(0.0, interval - elapsed)
        await _sleep_with_stop(remaining, stop_event)
    logger.info("Email inbox poller stopped.")


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


async def _run_one_tick() -> None:
    """One scan of every assistant with a connected Google account."""
    settings = get_settings()
    if not settings.AI_EMAIL_INBOX_ENABLED:
        return

    # Snapshot the candidate (assistant_id, granted_email, family_id)
    # tuples in a short read-only transaction so we don't hold a session
    # open across the (slow) Gmail calls.
    candidates: List[Tuple[int, str, int]] = []
    with _session() as db:
        rows = db.execute(
            select(models.GoogleOAuthCredential, models.Assistant)
            .join(
                models.Assistant,
                models.Assistant.assistant_id
                == models.GoogleOAuthCredential.assistant_id,
            )
        ).all()
        for cred, assistant in rows:
            scopes = (cred.scopes or "").split()
            has_modify = any(s.endswith("/gmail.modify") for s in scopes)
            if not has_modify:
                # Old grant with only gmail.send — can't read the inbox.
                # We log once at INFO so the operator notices.
                continue
            candidates.append(
                (assistant.assistant_id, cred.granted_email, assistant.family_id)
            )

    for assistant_id, granted_email, family_id in candidates:
        try:
            with _session() as db:
                await asyncio.to_thread(
                    process_assistant_inbox,
                    db,
                    assistant_id=assistant_id,
                    granted_email=granted_email,
                    family_id=family_id,
                )
        except Exception:  # noqa: BLE001 - per-assistant isolation
            logger.exception(
                "Email inbox processing failed for assistant_id=%s", assistant_id
            )


def _session() -> Session:
    """Tiny context-manager-friendly helper for ad-hoc DB sessions."""
    return SessionLocal()


# ---------------------------------------------------------------------------
# Per-assistant tick
# ---------------------------------------------------------------------------


def process_assistant_inbox(
    db: Session,
    *,
    assistant_id: int,
    granted_email: str,
    family_id: int,
) -> None:
    """Pull unread mail for one assistant, dispatch the agent, send replies.

    Synchronous on purpose — every call to Google's client library is
    blocking, so we let the outer ``asyncio.to_thread`` give us a
    background thread per assistant. SQLAlchemy session is also
    intrinsically synchronous, which keeps this code tidy.
    """
    settings = get_settings()
    try:
        cred_row, creds = google_oauth.load_credentials(db, assistant_id)
    except google_oauth.GoogleNotConnected:
        return
    except google_oauth.GoogleOAuthError as exc:
        logger.warning(
            "Email inbox: cannot load Google creds for assistant_id=%s: %s",
            assistant_id,
            exc,
        )
        return
    db.commit()  # persist any token-refresh side effect right away.

    try:
        message_ids = gmail.list_unread_inbox_message_ids(
            creds, max_results=settings.AI_EMAIL_INBOX_MAX_PER_TICK
        )
    except gmail.GmailReadError as exc:
        logger.warning(
            "Email inbox: list_unread failed for assistant_id=%s: %s",
            assistant_id,
            exc,
        )
        return

    if not message_ids:
        return

    logger.info(
        "Email inbox tick: assistant_id=%s found %d unread message(s)",
        assistant_id,
        len(message_ids),
    )

    for message_id in message_ids:
        # Dedup at the storage layer FIRST so we never re-spend an LLM
        # call for a message we already settled in a previous tick. We
        # still mark it read defensively to keep the unread queue clean.
        if _already_processed(db, assistant_id, message_id):
            try:
                gmail.mark_message_read(creds, message_id)
            except gmail.GmailReadError:
                pass
            continue

        try:
            _handle_one_message(
                db,
                assistant_id=assistant_id,
                granted_email=granted_email.lower(),
                family_id=family_id,
                creds=creds,
                gmail_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001 - per-message isolation
            logger.exception(
                "Email inbox: failed to handle message %s for assistant_id=%s",
                message_id,
                assistant_id,
            )
            # Best-effort failure receipt, then leave the message UNREAD
            # so a future code fix can reprocess it.
            try:
                _record_failure(
                    db,
                    assistant_id=assistant_id,
                    gmail_message_id=message_id,
                    reason=str(exc),
                )
            except Exception:  # noqa: BLE001 - don't mask original
                db.rollback()


def _already_processed(
    db: Session, assistant_id: int, gmail_message_id: str
) -> bool:
    return (
        db.execute(
            select(models.EmailInboxMessage.email_inbox_message_id)
            .where(models.EmailInboxMessage.assistant_id == assistant_id)
            .where(models.EmailInboxMessage.gmail_message_id == gmail_message_id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _record_failure(
    db: Session,
    *,
    assistant_id: int,
    gmail_message_id: str,
    reason: str,
) -> None:
    """Insert a minimal failure receipt when we don't even have the message."""
    row = models.EmailInboxMessage(
        assistant_id=assistant_id,
        gmail_message_id=gmail_message_id,
        gmail_thread_id="",
        sender_email="",
        sender_name=None,
        subject=None,
        body_excerpt=None,
        person_id=None,
        status="failed",
        status_reason=reason[:1000],
        received_at=_utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


# ---------------------------------------------------------------------------
# Per-message pipeline
# ---------------------------------------------------------------------------


def _handle_one_message(
    db: Session,
    *,
    assistant_id: int,
    granted_email: str,
    family_id: int,
    creds,
    gmail_message_id: str,
) -> None:
    msg = gmail.fetch_message(creds, gmail_message_id)
    body_excerpt = (msg.body_text or "")[:4000]
    common_audit = dict(
        assistant_id=assistant_id,
        gmail_message_id=msg.message_id,
        gmail_thread_id=msg.thread_id,
        sender_email=msg.sender_email,
        sender_name=msg.sender_name,
        subject=msg.subject,
        body_excerpt=body_excerpt,
        received_at=msg.received_at or _utcnow(),
    )

    # ---- Security gate 1 — self loop ----------------------------------
    if msg.sender_email == granted_email:
        _save_audit(
            db,
            **common_audit,
            person_id=None,
            status="ignored_self",
            status_reason="Sender matches Avi's connected Google account.",
        )
        _safe_mark_read(creds, gmail_message_id)
        return

    # ---- Security gate 2 — bulk / list / auto-reply -------------------
    if _is_bulk(msg):
        _save_audit(
            db,
            **common_audit,
            person_id=None,
            status="ignored_bulk",
            status_reason=_bulk_reason(msg),
        )
        _safe_mark_read(creds, gmail_message_id)
        return

    # ---- Security gate 3 — must match a registered family member ------
    person = _lookup_family_member_by_email(db, family_id, msg.sender_email)
    if person is None:
        logger.info(
            "Email inbox: ignoring unknown sender %r (assistant_id=%s, message=%s)",
            msg.sender_email,
            assistant_id,
            msg.message_id,
        )
        _save_audit(
            db,
            **common_audit,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=(
                f"No Person.email_address in family_id={family_id} matches "
                f"{msg.sender_email!r}."
            ),
        )
        _safe_mark_read(creds, gmail_message_id)
        return

    # ---- Sender is a registered family member — run the agent ---------
    logger.info(
        "Email inbox: replying to %r (person_id=%s) about subject=%r",
        msg.sender_email,
        person.person_id,
        msg.subject,
    )

    session, _created = live_session.find_or_create_email_session(
        db,
        family_id=family_id,
        external_thread_id=msg.thread_id,
        subject=msg.subject,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)

    inbound_text = _format_inbound_for_log(msg)
    live_session.log_message(
        db,
        session,
        role="user",
        content=inbound_text,
        person_id=person.person_id,
        meta={
            "kind": "email",
            "gmail_message_id": msg.message_id,
            "subject": msg.subject,
            "sender_email": msg.sender_email,
        },
    )

    # The agent task is created BEFORE the LLM run so a UI tail of
    # ``agent_tasks`` can spot the row appear in real time.
    task = agent_loop.create_task(
        db,
        family_id=family_id,
        live_session_id=session.live_session_id,
        person_id=person.person_id,
        kind="email",
        input_text=msg.body_text or "",
        model=ollama._model(),
    )
    db.commit()

    user_message = _format_user_message_for_agent(msg, person)
    history: List[dict] = []  # email is one-shot per turn, no in-memory history

    # ---- Fast-path web-search shortcut --------------------------------
    # When the user emails Avi asking a pure web-lookup question
    # ("What's the latest on the Fed rate decision?"), short-circuit
    # the heavy agent and reply with Gemini's grounded answer
    # directly. The classifier sees the raw body (subject is
    # informational metadata only); failure / "no" → fall through to
    # the heavy agent untouched.
    agent_failed = False
    final_text: Optional[str] = None
    shortcut_used = False
    try:
        shortcut_text = web_search_shortcut.try_shortcut_sync(
            (msg.body_text or "").strip()
        )
    except Exception:  # noqa: BLE001 - shortcut must never break the inbound
        logger.exception(
            "Email inbox: web-search shortcut crashed for message_id=%s "
            "— falling through to heavy agent",
            msg.message_id,
        )
        shortcut_text = None
    if shortcut_text:
        logger.info(
            "Email inbox: web-search shortcut handled message_id=%s "
            "task=%s (skipping heavy agent).",
            msg.message_id,
            task.agent_task_id,
        )
        final_text = shortcut_text
        shortcut_used = True

    if not shortcut_used:
        system_prompt = _build_email_system_prompt(
            db,
            family_id=family_id,
            person=person,
            sender_email=msg.sender_email,
            subject=msg.subject,
            assistant_id=assistant_id,
        )

        # Drive the agent loop synchronously by draining the async
        # generator. Same contract as the SMS / chat paths: every
        # inbound from a registered family member gets a reply, full
        # stop. If the agent itself crashes (LLM offline, tool
        # exception, asyncio glitch, …) we still send a short, honest
        # fallback so the sender knows we received their email and
        # aren't silently dropping it.
        try:
            final_text = _run_agent_to_completion(
                task_id=task.agent_task_id,
                family_id=family_id,
                assistant_id=assistant_id,
                person_id=person.person_id,
                system_prompt=system_prompt,
                history=history,
                user_message=user_message,
            )
        except Exception:  # noqa: BLE001 - last-ditch catch so we always reply
            logger.exception(
                "Email inbox: agent loop crashed for message_id=%s task=%s",
                msg.message_id,
                task.agent_task_id,
            )
            agent_failed = True
            final_text = (
                "Hi — Avi here. I got your message but hit a snag on my end "
                "and couldn't put together a real answer just now. I'll look "
                "into it and follow up as soon as I can."
            )

    # ---- Send the reply -----------------------------------------------
    reply_subject = msg.subject or "(no subject)"
    in_reply_to = msg.in_reply_to_header or msg.message_id
    try:
        reply_id = gmail.send_reply(
            creds,
            to=msg.sender_email,
            subject=reply_subject,
            body=final_text or "(Avi had nothing to add.)",
            in_reply_to=in_reply_to,
            references=msg.references_header,
            thread_id=msg.thread_id,
        )
    except gmail.GmailSendError as exc:
        logger.exception(
            "Email inbox: send_reply failed for message_id=%s", msg.message_id
        )
        _save_audit(
            db,
            **common_audit,
            person_id=person.person_id,
            status="failed",
            status_reason=f"send_reply: {exc}",
            agent_task_id=task.agent_task_id,
            live_session_id=session.live_session_id,
        )
        # Leave UNREAD so a future fix can reprocess it.
        return

    # ---- Log the reply + flip the audit row to success -----------------
    live_session.log_message(
        db,
        session,
        role="assistant",
        content=final_text,
        person_id=person.person_id,
        meta={
            "kind": "email_reply",
            "agent_task_id": task.agent_task_id,
            "gmail_message_id": reply_id,
            "in_reply_to": msg.message_id,
        },
    )
    if agent_failed:
        status_reason: Optional[str] = "Agent loop crashed; sent fallback apology."
    elif shortcut_used:
        status_reason = "Web-search shortcut handled (skipped heavy agent)."
    else:
        status_reason = None
    _save_audit(
        db,
        **common_audit,
        person_id=person.person_id,
        status="processed_replied",
        status_reason=status_reason,
        reply_message_id=reply_id,
        agent_task_id=task.agent_task_id,
        live_session_id=session.live_session_id,
    )

    _safe_mark_read(creds, gmail_message_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _safe_mark_read(creds, message_id: str) -> None:
    try:
        gmail.mark_message_read(creds, message_id)
    except gmail.GmailReadError as exc:
        logger.warning("Email inbox: mark_read failed for %s: %s", message_id, exc)


def _is_bulk(msg: gmail.FetchedMessage) -> bool:
    if msg.list_id_header:
        return True
    if (msg.precedence_header or "").lower() in {"bulk", "list", "junk"}:
        return True
    if msg.auto_submitted_header and msg.auto_submitted_header.lower() != "no":
        return True
    return False


def _bulk_reason(msg: gmail.FetchedMessage) -> str:
    bits = []
    if msg.list_id_header:
        bits.append(f"List-Id={msg.list_id_header!r}")
    if msg.precedence_header:
        bits.append(f"Precedence={msg.precedence_header!r}")
    if msg.auto_submitted_header:
        bits.append(f"Auto-Submitted={msg.auto_submitted_header!r}")
    return "Looks like bulk / auto-reply mail (" + ", ".join(bits) + ")"


def _lookup_family_member_by_email(
    db: Session, family_id: int, email: str
) -> Optional[models.Person]:
    if not email:
        return None
    # Case-insensitive exact match against EITHER the personal or
    # work mailbox so a family member writing from their work email
    # is still recognised. We don't strip plus-tags or domain
    # aliases on purpose — if the user sends from
    # ben+work@example.com we want them to register that exact
    # alias rather than have Avi silently broaden the security gate.
    return db.execute(
        select(models.Person)
        .where(models.Person.family_id == family_id)
        .where(
            models.Person.email_address.ilike(email)
            | models.Person.work_email.ilike(email)
        )
        .limit(1)
    ).scalar_one_or_none()


def _save_audit(db: Session, **kwargs) -> models.EmailInboxMessage:
    """Insert (or, on dedup, fetch) the email_inbox_messages row.

    A race where two pollers reach for the same message at the same
    time is handled by the unique constraint — the loser catches the
    integrity error, rolls back, and treats the row as already done.
    """
    row = models.EmailInboxMessage(**kwargs)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(models.EmailInboxMessage)
            .where(
                models.EmailInboxMessage.assistant_id == kwargs["assistant_id"]
            )
            .where(
                models.EmailInboxMessage.gmail_message_id
                == kwargs["gmail_message_id"]
            )
            .limit(1)
        ).scalar_one()
        return existing
    return row


def _format_inbound_for_log(msg: gmail.FetchedMessage) -> str:
    """Render the email so it's readable when replayed in the history view."""
    bits = [
        f"Subject: {msg.subject or '(no subject)'}",
        f"From: {msg.sender_name + ' <' + msg.sender_email + '>' if msg.sender_name else msg.sender_email}",
        "",
        msg.body_text or "(no body)",
    ]
    return "\n".join(bits)


def _format_user_message_for_agent(
    msg: gmail.FetchedMessage, person: models.Person
) -> str:
    """Wrap the email so the agent knows what surface this came through."""
    name = person.preferred_name or person.first_name or msg.sender_email
    return (
        f"[Email from {name} <{msg.sender_email}>]\n"
        f"Subject: {msg.subject or '(no subject)'}\n\n"
        f"{(msg.body_text or '').strip() or '(no body)'}"
    )


def _build_email_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    sender_email: str,
    subject: Optional[str],
    assistant_id: int,
) -> str:
    """Mirror the live-chat system prompt, but tuned for an email reply.

    The biggest difference is the trailing "How to reply" block — we
    want a self-contained reply paragraph (not a chat snippet) and we
    want the agent to be very conservative with tools (one round of
    lookups, then write the reply).
    """
    family = db.get(models.Family, family_id)
    assistant_name = family.assistant.assistant_name if family and family.assistant else "Avi"
    family_name = family.family_name if family else None

    # Email sender is the speaker for authz purposes — we identified
    # them by their registered email address, the same role face
    # recognition plays on the live page. RAG redaction below uses
    # this so a child sending Avi an email cannot extract a parent's
    # SSN by typing the question into Gmail instead of speaking it.
    rag_block = ""
    if family is not None:
        rag_block = rag.build_family_overview(
            db, family, requestor_person_id=person.person_id
        )
    person_block = "Currently emailing with:\n" + rag.build_person_context(
        db, person, requestor_person_id=person.person_id
    )

    registry = agent_tools.build_default_registry()
    capabilities = agent_tools.detect_capabilities(db, assistant_id)
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
    parts.append(
        "--- How to reply to this email ---\n"
        f"This message arrived via email from {sender_email}. Your final "
        f"answer will be sent verbatim as the body of an email reply on "
        f"the subject {subject or '(no subject)'!r}. Therefore:\n"
        "* Write a complete, self-contained reply paragraph (or short "
        "  list). It is not a chat snippet — no 'Sure!' or 'Great!' "
        "  openers, no trailing 'let me know if I can help'.\n"
        "* Sign off as the assistant (use the assistant's name, not a "
        "  human's name).\n"
        "* Use at most one round of tool calls before writing the "
        "  reply. Email is asynchronous — if you need more info, ask "
        "  the user one specific question and stop.\n"
        "* NEVER include the user's encrypted identifiers, passwords, "
        "  or anything ending in _encrypted in the reply body.\n"
    )
    return prompts.with_safety("\n\n".join(parts))


def _run_agent_to_completion(
    *,
    task_id: int,
    family_id: int,
    assistant_id: int,
    person_id: int,
    system_prompt: str,
    history: List[dict],
    user_message: str,
) -> str:
    """Drain the async agent generator and return the final reply text.

    The agent already persists every step + the final ``summary`` to
    ``agent_tasks`` / ``agent_steps``, so we just need the text the
    model actually settled on. We pull it from the ``task_completed``
    event (preferred) or, on failure, return the canonical apology so
    the recipient still hears back.
    """
    registry = agent_tools.build_default_registry()
    # We open a fresh session here because ``run_agent`` opens its own
    # too — that is by design (agent loop is HTTP-request-lifetime
    # independent), so passing our DB session in would be wrong.
    with _session() as db_for_caps:
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
        # We're inside an already-running event loop (shouldn't happen
        # because the caller used asyncio.to_thread, but be defensive).
        # Spin up a private loop on this thread to host the run.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drain())
        finally:
            loop.close()

    if error_text and not final_text:
        return (
            "Hi — Avi here. I tried to help with your message but ran into a "
            "problem on the local server. The household admin has been "
            "notified; please try again in a bit."
        )
    return final_text or "Hi — Avi here. I read your note but didn't have anything to add right now."


__all__ = [
    "run_email_inbox_loop",
    "process_assistant_inbox",
]
