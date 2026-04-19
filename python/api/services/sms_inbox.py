"""SMS-driven AI assistant — Twilio webhook handler + per-message agent dispatch.

Big picture
-----------
This module turns Avi into an **SMS auto-responder for registered
family members only**. Unlike the email path (which polls Gmail every
60 s) Twilio pushes — :func:`process_inbound_sms` is invoked
synchronously from the ``POST /api/sms/twilio/inbound`` webhook
handler in ``routers/sms_webhook.py``.

* Inbound webhook is signature-verified (see
  :func:`api.integrations.twilio_sms.verify_twilio_signature`) before
  we ever look at the form contents — anything Twilio didn't sign is
  dropped at the router layer.
* Dedup happens at the storage layer (``twilio_message_sid`` UNIQUE),
  so a Twilio retry never re-spends an LLM call.
* The sender's phone number is matched against
  ``people.{mobile,home,work}_phone_number`` for every family in the
  database. **All unmatched senders are silently ignored, recorded in
  ``sms_inbox_messages`` with ``status='ignored_unknown_sender'``.**
  This is the single security gate — no other code path replies to
  SMS.
* When the sender does match a registered person we open / reuse a
  ``LiveSession`` keyed on the counterparty's E.164 phone, log the
  inbound message into the transcript, run the same agent loop the
  live chat uses, and send the final answer back via Twilio's REST
  API.

Architectural parity with email
-------------------------------
Identical flow to ``services.email_inbox._handle_one_message`` —
self-loop gate → "is this an opt-out keyword?" gate → person-lookup
gate → session/transcript bookkeeping → agent loop → reply send →
audit row finalisation.

What this code DELIBERATELY does not do
---------------------------------------
* Reply to anyone whose phone number does not match (after E.164
  normalisation) one ``Person.{mobile,home,work}_phone_number``.
* Reply to STOP / UNSUBSCRIBE / END keywords. Twilio handles the
  carrier-level opt-out; we still record the row but never fire
  the agent loop.
* Loop on its own outbound traffic. If an inbound's ``From`` matches
  ``TWILIO_PRIMARY_PHONE`` it's recorded as ``ignored_self`` and
  dropped.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai import agent as agent_loop
from ..ai import authz
from ..ai import ollama, prompts, rag, schema_catalog
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..config import get_settings
from ..db import SessionLocal
from ..integrations import twilio_sms
from ..utils.phone import normalize_phone


logger = logging.getLogger(__name__)


# Per Twilio's documentation, these single-word bodies (case-insensitive)
# trigger the carrier-level opt-out chain. We never want to send a
# reply that races Twilio's own STOP confirmation, so we treat them as
# a hard ignore.
_OPT_OUT_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public entry point — invoked from the webhook router
# ---------------------------------------------------------------------------


def process_inbound_sms(
    db: Session,
    inbound: twilio_sms.InboundSms,
) -> models.SmsInboxMessage:
    """Resolve a parsed Twilio webhook to an audit row + (maybe) a reply.

    The router calls this synchronously inside its request handler. We
    keep it sync because every downstream piece (SQLAlchemy, the
    agent's own ``asyncio.run``) is already sync-or-blocking and that
    matches the way ``services.email_inbox._handle_one_message`` works.
    """
    settings = get_settings()
    common = dict(
        twilio_message_sid=inbound.message_sid,
        twilio_messaging_service_sid=inbound.messaging_service_sid,
        from_phone=inbound.from_phone,
        to_phone=inbound.to_phone,
        body=inbound.body,
        num_media=inbound.num_media,
        received_at=_utcnow(),
    )

    if not inbound.message_sid:
        logger.warning("Twilio webhook missing MessageSid; refusing to process.")
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="failed",
            status_reason="Webhook missing MessageSid.",
            **common,
        )

    # ---- Dedup -------------------------------------------------------
    if _already_processed(db, inbound.message_sid):
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_already_seen",
            status_reason="Webhook is a Twilio retry — original already processed.",
            **common,
        )

    # ---- Self-loop ---------------------------------------------------
    own_number = normalize_phone(settings.TWILIO_PRIMARY_PHONE)
    if own_number and normalize_phone(inbound.from_phone) == own_number:
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_self",
            status_reason="Sender matches our own Twilio number.",
            **common,
        )

    # ---- Opt-out keyword --------------------------------------------
    if _is_opt_out(inbound.body):
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_stop",
            status_reason=(
                f"Opt-out keyword received ({inbound.body.strip()!r}); "
                "Twilio handles the carrier-level opt-out."
            ),
            **common,
        )

    # ---- Person lookup ----------------------------------------------
    person = _lookup_family_member_by_phone(db, inbound.from_phone)
    if person is None:
        logger.info(
            "SMS inbox: ignoring unknown sender %r (sid=%s)",
            inbound.from_phone,
            inbound.message_sid,
        )
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_unknown_sender",
            status_reason=(
                f"No Person.{{mobile,home,work}}_phone_number across any "
                f"family matches {inbound.from_phone!r}."
            ),
            **common,
        )

    # ---- Open / reuse the SMS session -------------------------------
    counterparty = normalize_phone(inbound.from_phone) or inbound.from_phone
    session, _created = live_session.find_or_create_sms_session(
        db,
        family_id=person.family_id,
        counterparty_phone=counterparty,
    )
    live_session.upsert_participant(db, session, person_id=person.person_id)

    # Insert the audit row early (status='failed') so a downstream
    # crash still leaves a forensic trail. We'll flip its status
    # afterwards.
    audit = _save_audit(
        db,
        family_id=person.family_id,
        person_id=person.person_id,
        status="failed",
        status_reason="Pipeline started but did not complete; see logs.",
        live_session_id=session.live_session_id,
        **common,
    )

    # ---- Download MMS media (if any) and persist + log -------------
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
            "kind": "sms",
            "twilio_message_sid": inbound.message_sid,
            "from_phone": inbound.from_phone,
            "to_phone": inbound.to_phone,
            "attachments": attachments_meta or None,
        },
    )

    # ---- Skip the agent loop if SMS is disabled --------------------
    if not settings.AI_SMS_INBOUND_ENABLED:
        audit.status = "failed"
        audit.status_reason = "AI_SMS_INBOUND_ENABLED=false"
        db.commit()
        return audit

    # ---- Run the agent ---------------------------------------------
    task = agent_loop.create_task(
        db,
        family_id=person.family_id,
        live_session_id=session.live_session_id,
        person_id=person.person_id,
        kind="sms",
        input_text=inbound.body or "",
        model=ollama._model(),
    )
    audit.agent_task_id = task.agent_task_id
    db.commit()

    system_prompt = _build_sms_system_prompt(
        db,
        family_id=person.family_id,
        person=person,
        from_phone=inbound.from_phone,
    )
    user_message = _format_user_message_for_agent(inbound, person)

    # The contract with the user is: every inbound SMS from a registered
    # family member gets a reply, period. Even if the agent loop crashes
    # (LLM offline, tool exception, asyncio glitch, …) we still want
    # SOMETHING to land on their phone so they know we received the
    # message and aren't silently dropping it. So: catch everything here
    # and fall back to a short, honest "I tried but hit a snag" string —
    # the audit row + the agent_task row keep the full forensic trail
    # for debugging.
    agent_failed = False
    try:
        final_text = _run_agent_to_completion(
            task_id=task.agent_task_id,
            family_id=person.family_id,
            assistant_id=_assistant_id_for_family(db, person.family_id),
            person_id=person.person_id,
            system_prompt=system_prompt,
            history=[],
            user_message=user_message,
        )
    except Exception:  # noqa: BLE001 - last-ditch catch so we always reply
        logger.exception(
            "SMS inbox: agent loop crashed for sid=%s task=%s",
            inbound.message_sid,
            task.agent_task_id,
        )
        agent_failed = True
        final_text = (
            "Got your text — Avi here. I hit a snag on my end and "
            "couldn't finish that just now. I'll look into it."
        )

    final_text = twilio_sms.truncate_for_sms(
        final_text or "Got your text — nothing to add right now.",
        max_chars=settings.AI_SMS_REPLY_MAX_CHARS,
    )

    # ---- Send the reply via Twilio ---------------------------------
    if not settings.TWILIO_PRIMARY_PHONE:
        audit.status = "failed"
        audit.status_reason = "TWILIO_PRIMARY_PHONE not configured."
        db.commit()
        return audit

    try:
        reply_sid = twilio_sms.send_sms(
            account_sid=settings.TWILIO_ACCOUNT_SID or "",
            auth_token=settings.TWILIO_AUTH_TOKEN or "",
            from_phone=settings.TWILIO_PRIMARY_PHONE,
            to_phone=inbound.from_phone,
            body=final_text,
        )
    except twilio_sms.TwilioSendError as exc:
        logger.exception("SMS inbox: send_sms failed for sid=%s", inbound.message_sid)
        audit.status = "failed"
        audit.status_reason = f"send_sms: {exc}"
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
            "kind": "sms_reply",
            "agent_task_id": task.agent_task_id,
            "twilio_message_sid": reply_sid,
            "in_reply_to": inbound.message_sid,
        },
    )
    if agent_failed:
        # We did get a reply onto the user's phone, but the agent itself
        # did not produce a real answer — flag the audit row so the admin
        # UI's status pill is honest about what happened.
        audit.status = "processed_replied"
        audit.status_reason = "Agent loop crashed; sent fallback apology."
    else:
        audit.status = "processed_replied"
        audit.status_reason = None
    audit.reply_message_sid = reply_sid
    db.commit()
    return audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _already_processed(db: Session, twilio_message_sid: str) -> bool:
    return (
        db.execute(
            select(models.SmsInboxMessage.sms_inbox_message_id)
            .where(models.SmsInboxMessage.twilio_message_sid == twilio_message_sid)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _is_opt_out(body: Optional[str]) -> bool:
    if not body:
        return False
    return body.strip().lower() in _OPT_OUT_KEYWORDS


def _lookup_family_member_by_phone(
    db: Session, phone: str
) -> Optional[models.Person]:
    """Find the unique person whose phone matches ``phone`` (E.164).

    SMS goes to one Twilio number for the whole household app, so we
    can't pre-filter by family the way the email poller does. Instead
    we normalise once, fetch every person whose stored numbers might
    plausibly match (cheap with the index on from_phone+name), then
    do the strict equality check in Python so we tolerate the
    historic "(415) 555-1234" / "415-555-1234" / "+14155551234"
    variants without needing a migration.

    If two family members share a number (e.g. shared landline) we
    return the first one — the audit row still records the actual
    inbound number so the operator can disambiguate later.
    """
    target = normalize_phone(phone)
    if target is None:
        return None
    last10 = target[-10:]  # cheap pre-filter regardless of formatting
    candidates = db.execute(
        select(models.Person).where(
            or_(
                models.Person.mobile_phone_number.ilike(f"%{last10}%"),
                models.Person.home_phone_number.ilike(f"%{last10}%"),
                models.Person.work_phone_number.ilike(f"%{last10}%"),
            )
        )
    ).scalars().all()
    for p in candidates:
        for raw in (
            p.mobile_phone_number,
            p.home_phone_number,
            p.work_phone_number,
        ):
            if normalize_phone(raw) == target:
                return p
    return None


def _save_audit(db: Session, **kwargs) -> models.SmsInboxMessage:
    """Insert (or, on dedup, fetch) the sms_inbox_messages row."""
    row = models.SmsInboxMessage(**kwargs)
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(models.SmsInboxMessage)
            .where(
                models.SmsInboxMessage.twilio_message_sid
                == kwargs["twilio_message_sid"]
            )
            .limit(1)
        ).scalar_one()
        return existing


def _persist_attachments(
    db: Session,
    audit: models.SmsInboxMessage,
    inbound: twilio_sms.InboundSms,
) -> List[dict]:
    """Download every MMS media file and write SmsInboxAttachment rows.

    Returns a list of ``{index, mime_type, stored_path, file_size_bytes}``
    dicts so the caller can mention them in the transcript meta.
    Failures on one media file don't block the others — we log and
    keep going so a busted CDN URL doesn't drop the whole reply.
    """
    if not inbound.media or audit.family_id is None:
        return []
    settings = get_settings()
    out: List[dict] = []
    for index, url, _hint_mime in inbound.media:
        try:
            blob = twilio_sms.download_media(
                account_sid=settings.TWILIO_ACCOUNT_SID or "",
                auth_token=settings.TWILIO_AUTH_TOKEN or "",
                media_url=url,
                media_index=index,
            )
        except twilio_sms.TwilioMediaError as exc:
            logger.warning(
                "SMS inbox: failed to fetch media #%d for sid=%s: %s",
                index,
                inbound.message_sid,
                exc,
            )
            continue
        rel_path, size = storage.save_sms_attachment(
            family_id=audit.family_id,
            sms_inbox_message_id=audit.sms_inbox_message_id,
            file_bytes=blob.file_bytes,
            extension=twilio_sms.extension_for_mime(blob.mime_type),
        )
        att = models.SmsInboxAttachment(
            sms_inbox_message_id=audit.sms_inbox_message_id,
            media_index=index,
            twilio_media_url=url,
            mime_type=blob.mime_type,
            file_size_bytes=size,
            stored_path=rel_path,
        )
        db.add(att)
        out.append(
            {
                "media_index": index,
                "mime_type": blob.mime_type,
                "stored_path": rel_path,
                "file_size_bytes": size,
            }
        )
    if out:
        db.flush()
    return out


def _format_inbound_for_log(
    inbound: twilio_sms.InboundSms, attachments: List[dict]
) -> str:
    """Render an inbound SMS so it reads naturally in the history view."""
    lines = [
        f"From: {inbound.from_phone}",
        f"To: {inbound.to_phone}",
        "",
        inbound.body or "(no body)",
    ]
    if attachments:
        lines.append("")
        for att in attachments:
            lines.append(
                f"[attachment {att['media_index']}: {att['mime_type']}, "
                f"{att['file_size_bytes']} bytes]"
            )
    return "\n".join(lines)


def _format_user_message_for_agent(
    inbound: twilio_sms.InboundSms, person: models.Person
) -> str:
    """Wrap the SMS so the agent knows what surface this came through."""
    name = person.preferred_name or person.first_name or inbound.from_phone
    media_hint = ""
    if inbound.num_media:
        media_hint = (
            f" (with {inbound.num_media} attached "
            f"{'image' if inbound.num_media == 1 else 'images'})"
        )
    return (
        f"[Text message from {name} <{inbound.from_phone}>{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no body)'}"
    )


def _build_sms_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    from_phone: str,
) -> str:
    """Mirror the email system prompt, but tuned for an SMS reply.

    Differs from the email build:
    * Reply must fit in ~480 characters.
    * No subject line, no sign-off — the recipient already knows it's
      Avi (one number, ongoing thread).
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
    person_block = "Currently texting with:\n" + rag.build_person_context(
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
    max_chars = get_settings().AI_SMS_REPLY_MAX_CHARS
    parts.append(
        "--- How to reply to this text message ---\n"
        f"This message arrived as an SMS from {from_phone}. Your final "
        f"answer will be sent verbatim as the body of an SMS reply. "
        "Therefore:\n"
        f"* Keep the reply UNDER {max_chars} characters — ideally one "
        "  or two short sentences. Texts are read on a phone screen; "
        "  brevity reads as competence.\n"
        "* Plain text only. No Markdown, no bullet lists, no asterisks.\n"
        "* No subject line, no greeting, no 'Sincerely, Avi'. The "
        "  recipient knows who you are; jump straight to the answer.\n"
        "* Use at most one round of tool calls before writing the "
        "  reply. SMS is asynchronous — if you need more info, ask "
        "  the user one specific question and stop.\n"
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

    Mirrors ``services.email_inbox._run_agent_to_completion`` exactly
    so the two paths share behaviour.
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
        return "Sorry — Avi here. I tried but my server hit a snag. Try again in a bit."
    return final_text or "Got your text — nothing to add right now."


__all__ = ["process_inbound_sms"]
