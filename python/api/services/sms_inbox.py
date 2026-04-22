"""SMS / WhatsApp Twilio webhook handler + per-message agent dispatch.

Big picture
-----------
This module turns Avi into an **SMS / WhatsApp auto-responder for
registered family members only**. Unlike the email path (which polls
Gmail every 60 s) Twilio pushes — :func:`process_inbound_sms` is
invoked synchronously from the ``POST /api/sms/twilio/inbound``
webhook handler in ``routers/sms_webhook.py``. The same webhook /
service handles both surfaces because Twilio's WhatsApp Programmable
Messaging API is a strict superset of SMS: same payload, same
``MessageSid`` namespace, same Basic-auth credentials. Inbound rows
are tagged with ``channel='sms' | 'whatsapp'`` (parsed from the
``whatsapp:`` prefix on the ``From`` address) and the per-channel
divergences (sender number, length cap, env flag, live-session
``source``) are encapsulated in :func:`_channel_config_for`.

* Inbound webhook is signature-verified (see
  :func:`api.integrations.twilio_sms.verify_twilio_signature`) before
  we ever look at the form contents — anything Twilio didn't sign is
  dropped at the router layer.
* Dedup happens at the storage layer (``twilio_message_sid`` UNIQUE),
  so a Twilio retry never re-spends an LLM call. The same SID space
  is shared across SMS and WhatsApp, which is exactly what we want.
* The sender's phone number is matched against
  ``people.{mobile,home,work}_phone_number`` for every family in the
  database, after stripping the ``whatsapp:`` prefix so the same
  person reaching us on SMS and on WhatsApp resolves to the same
  household member. **All unmatched senders are silently ignored,
  recorded in ``sms_inbox_messages`` with
  ``status='ignored_unknown_sender'``.** This is the single security
  gate — no other code path replies to either surface.
* When the sender does match a registered person we open / reuse a
  ``LiveSession`` keyed on the counterparty's E.164 phone with
  ``source='sms'`` or ``source='whatsapp'`` so the two surfaces keep
  separate transcripts (different reply-length norms, different
  opt-in conventions), log the inbound into the transcript, run the
  same agent loop the live chat uses, and send the final answer back
  via Twilio's REST API (``send_sms`` / ``send_whatsapp``).

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
* Loop on its own outbound traffic. If an inbound's ``From`` (after
  stripping ``whatsapp:``) matches ``TWILIO_PRIMARY_PHONE`` (for SMS)
  or ``TWILIO_WHATSAPP_SENDER_NUMBER`` (for WhatsApp) it's recorded
  as ``ignored_self`` and dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai import agent as agent_loop
from ..ai import agent_drain
from ..ai import assistants as _assistants
from ..ai import ollama
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..ai import web_search_shortcut
from ..config import Settings, get_settings
from ..db import SessionLocal
from ..integrations import twilio_sms
from ..utils.phone import normalize_phone
from . import inbound_prompts


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
# Per-channel configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ChannelConfig:
    """Per-channel knobs derived once per inbound message.

    Keeping all the SMS-vs-WhatsApp branches in one bundle (built up
    front by :func:`_channel_config_for`) makes the main pipeline read
    linearly — every later step just dereferences the right field
    instead of re-checking ``inbound.channel`` over and over.
    """

    channel: str  # 'sms' | 'whatsapp'
    label: str  # 'SMS' / 'WhatsApp' — for logs and prompt text
    inbound_kind: str  # transcript meta `kind` for the user-message log
    reply_kind: str  # transcript meta `kind` for the assistant log
    feature_enabled: bool  # AI_{SMS|WHATSAPP}_INBOUND_ENABLED
    feature_flag_name: str  # for status_reason wording
    sender_number: Optional[str]  # TWILIO_PRIMARY_PHONE / TWILIO_WHATSAPP_SENDER_NUMBER
    sender_env_name: str
    reply_max_chars: int
    agent_task_kind: str  # 'sms' / 'whatsapp'
    surface_description: str  # for the system prompt so the LLM knows where it landed


def _channel_config_for(
    inbound: twilio_sms.InboundSms, settings: Settings
) -> _ChannelConfig:
    """Resolve per-surface settings for one inbound Twilio webhook."""
    if inbound.channel == "whatsapp":
        return _ChannelConfig(
            channel="whatsapp",
            label="WhatsApp",
            inbound_kind="whatsapp",
            reply_kind="whatsapp_reply",
            feature_enabled=settings.AI_WHATSAPP_INBOUND_ENABLED,
            feature_flag_name="AI_WHATSAPP_INBOUND_ENABLED",
            sender_number=settings.TWILIO_WHATSAPP_SENDER_NUMBER,
            sender_env_name="TWILIO_WHATSAPP_SENDER_NUMBER",
            reply_max_chars=settings.AI_WHATSAPP_REPLY_MAX_CHARS,
            agent_task_kind="whatsapp",
            surface_description="WhatsApp message",
        )
    return _ChannelConfig(
        channel="sms",
        label="SMS",
        inbound_kind="sms",
        reply_kind="sms_reply",
        feature_enabled=settings.AI_SMS_INBOUND_ENABLED,
        feature_flag_name="AI_SMS_INBOUND_ENABLED",
        sender_number=settings.TWILIO_PRIMARY_PHONE,
        sender_env_name="TWILIO_PRIMARY_PHONE",
        reply_max_chars=settings.AI_SMS_REPLY_MAX_CHARS,
        agent_task_kind="sms",
        surface_description="SMS",
    )


def _send_for_channel(
    chan: _ChannelConfig,
) -> Callable[..., str]:
    """Return the right Twilio outbound function for ``chan``."""
    if chan.channel == "whatsapp":
        return twilio_sms.send_whatsapp
    return twilio_sms.send_sms


def _open_session_for_channel(
    db: Session,
    chan: _ChannelConfig,
    *,
    family_id: int,
    counterparty_phone: str,
) -> Tuple[models.LiveSession, bool]:
    """Open / reuse the right LiveSession row for ``chan``."""
    if chan.channel == "whatsapp":
        return live_session.find_or_create_whatsapp_session(
            db,
            family_id=family_id,
            counterparty_phone=counterparty_phone,
        )
    return live_session.find_or_create_sms_session(
        db,
        family_id=family_id,
        counterparty_phone=counterparty_phone,
    )


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
    chan = _channel_config_for(inbound, settings)

    # `From` may carry a `whatsapp:` prefix on WhatsApp inbound; strip
    # it once up front so every downstream phone-number consumer
    # (normalize, person lookup, session key, self-loop) sees the bare
    # E.164 form. The prefixed value is preserved on the audit row +
    # transcript so it's obvious which surface a message came from.
    from_phone_bare = twilio_sms.strip_whatsapp_prefix(inbound.from_phone)

    common = dict(
        channel=chan.channel,
        twilio_message_sid=inbound.message_sid,
        twilio_messaging_service_sid=inbound.messaging_service_sid,
        from_phone=inbound.from_phone,
        to_phone=inbound.to_phone,
        body=inbound.body,
        num_media=inbound.num_media,
        received_at=_utcnow(),
    )

    if not inbound.message_sid:
        logger.warning(
            "Twilio %s webhook missing MessageSid; refusing to process.",
            chan.label,
        )
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
    own_number = normalize_phone(chan.sender_number)
    if own_number and normalize_phone(from_phone_bare) == own_number:
        return _save_audit(
            db,
            family_id=None,
            person_id=None,
            status="ignored_self",
            status_reason=f"Sender matches our own Twilio {chan.label} number.",
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
    person = _lookup_family_member_by_phone(db, from_phone_bare)
    if person is None:
        logger.info(
            "%s inbox: ignoring unknown sender %r (sid=%s)",
            chan.label,
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

    # ---- Open / reuse the per-channel session ----------------------
    # Same person reaching us on SMS vs. WhatsApp lands on different
    # session rows because the surfaces have different reply-length
    # and tone conventions; we don't want one transcript to bleed into
    # the other.
    counterparty = normalize_phone(from_phone_bare) or from_phone_bare
    session, _created = _open_session_for_channel(
        db,
        chan,
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

    # ---- Download MMS / WhatsApp media (if any) and persist + log --
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
            "kind": chan.inbound_kind,
            "channel": chan.channel,
            "twilio_message_sid": inbound.message_sid,
            "from_phone": inbound.from_phone,
            "to_phone": inbound.to_phone,
            "attachments": attachments_meta or None,
        },
    )

    # ---- Skip the agent loop if this surface is disabled -----------
    if not chan.feature_enabled:
        audit.status = "failed"
        audit.status_reason = f"{chan.feature_flag_name}=false"
        db.commit()
        return audit

    # ---- Run the agent ---------------------------------------------
    task = agent_loop.create_task(
        db,
        family_id=person.family_id,
        live_session_id=session.live_session_id,
        person_id=person.person_id,
        kind=chan.agent_task_kind,
        input_text=inbound.body or "",
        model=ollama._model(),
    )
    audit.agent_task_id = task.agent_task_id
    db.commit()

    user_message = _format_user_message_for_agent(inbound, person, chan)

    # ---- Fast-path web-search shortcut ----------------------------
    # Skip the heavy agent entirely when the lightweight Gemma
    # classifier is confident this message is a pure web-lookup ask.
    # Saves ~5-10 s of round-trips. We hand it the RAW body (not the
    # `[Text message from ...]` wrapper) because the wrapper is a
    # surface hint for the heavy agent, not signal for the
    # classifier. ``try_shortcut_sync`` itself is total — every
    # failure mode returns ``None`` so we just check the return.
    agent_failed = False
    final_text: Optional[str] = None
    shortcut_used = False
    shortcut_text = web_search_shortcut.try_shortcut_sync(
        (inbound.body or "").strip()
    )
    if shortcut_text:
        logger.info(
            "%s inbox: web-search shortcut handled sid=%s task=%s "
            "(skipping heavy agent).",
            chan.label,
            inbound.message_sid,
            task.agent_task_id,
        )
        final_text = shortcut_text
        shortcut_used = True

    if not shortcut_used:
        system_prompt = _build_sms_system_prompt(
            db,
            family_id=person.family_id,
            person=person,
            from_phone=inbound.from_phone,
            chan=chan,
        )

        # The contract with the user is: every inbound message from a
        # registered family member gets a reply, period. Even if the
        # agent loop crashes (LLM offline, tool exception, asyncio
        # glitch, …) we still want SOMETHING to land on their phone
        # so they know we received the message and aren't silently
        # dropping it. So: catch everything here and fall back to a
        # short, honest "I tried but hit a snag" string — the audit
        # row + the agent_task row keep the full forensic trail for
        # debugging.
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
                "%s inbox: agent loop crashed for sid=%s task=%s",
                chan.label,
                inbound.message_sid,
                task.agent_task_id,
            )
            agent_failed = True
            final_text = (
                f"Got your message — Avi here. I hit a snag on my end "
                "and couldn't finish that just now. I'll look into it."
            )

    final_text = twilio_sms.truncate_for_sms(
        final_text or "Got your message — nothing to add right now.",
        max_chars=chan.reply_max_chars,
    )

    # ---- Send the reply via Twilio ---------------------------------
    if not chan.sender_number:
        audit.status = "failed"
        audit.status_reason = f"{chan.sender_env_name} not configured."
        db.commit()
        return audit

    send_func = _send_for_channel(chan)
    try:
        reply_sid = send_func(
            account_sid=settings.TWILIO_ACCOUNT_SID or "",
            auth_token=settings.TWILIO_AUTH_TOKEN or "",
            from_phone=chan.sender_number,
            # send_whatsapp adds the `whatsapp:` prefix idempotently;
            # send_sms uses the value verbatim. Either way, passing the
            # original prefixed `inbound.from_phone` is safe — the
            # WhatsApp sender helper will not double-prefix.
            to_phone=inbound.from_phone,
            body=final_text,
        )
    except twilio_sms.TwilioSendError as exc:
        logger.exception(
            "%s inbox: outbound send failed for sid=%s",
            chan.label,
            inbound.message_sid,
        )
        audit.status = "failed"
        audit.status_reason = f"twilio send: {exc}"
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
            "kind": chan.reply_kind,
            "channel": chan.channel,
            "agent_task_id": task.agent_task_id,
            "twilio_message_sid": reply_sid,
            "in_reply_to": inbound.message_sid,
            **({"shortcut": "web_search"} if shortcut_used else {}),
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
    inbound: twilio_sms.InboundSms,
    person: models.Person,
    chan: _ChannelConfig,
) -> str:
    """Wrap the inbound so the agent knows what surface this came through.

    Tagging the surface in the user-message header lets the agent
    instinctively right-size its reply (a one-line SMS reply vs. a
    chat-style WhatsApp reply) without us having to pile every nuance
    into the system prompt.
    """
    name = person.preferred_name or person.first_name or inbound.from_phone
    media_hint = ""
    if inbound.num_media:
        media_hint = (
            f" (with {inbound.num_media} attached "
            f"{'image' if inbound.num_media == 1 else 'images'})"
        )
    surface = "Text message" if chan.channel == "sms" else "WhatsApp message"
    return (
        f"[{surface} from {name} <{inbound.from_phone}>{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no body)'}"
    )


def _build_sms_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    from_phone: str,
    chan: _ChannelConfig,
) -> str:
    """Build the SMS / WhatsApp-flavoured system prompt.

    Uses :func:`inbound_prompts.build_inbound_system_prompt` for the
    common Avi/RAG/capability scaffolding. Only the surface verb and
    the trailing "how to reply" block vary, with the SMS / WhatsApp
    branch governed by ``chan``.
    """
    surface_verb = "texting with" if chan.channel == "sms" else "messaging on WhatsApp with"
    max_chars = chan.reply_max_chars
    if chan.channel == "whatsapp":
        how_to_reply = (
            f"--- How to reply to this WhatsApp message ---\n"
            f"This message arrived as a WhatsApp text from {from_phone}. "
            "Your final answer will be sent verbatim as the body of a "
            "WhatsApp reply via Twilio's Programmable Messaging API. "
            "Therefore:\n"
            f"* Keep the reply UNDER {max_chars} characters — WhatsApp "
            "  readers tolerate longer chat-style messages than SMS, "
            "  but two-three short paragraphs is the sweet spot.\n"
            "* Plain text only. No Markdown, no bullet lists, no "
            "  asterisks (WhatsApp doesn't render Markdown — asterisks "
            "  show up as literal stars).\n"
            "* No subject line, no greeting, no 'Sincerely, Avi'. The "
            "  recipient knows who you are; jump straight to the answer.\n"
            "* Use at most one round of tool calls before writing the "
            "  reply. WhatsApp is asynchronous — if you need more "
            "  info, ask the user one specific question and stop.\n"
            "* You're inside the 24-hour customer-care window opened "
            "  by the user's inbound. Free-form replies are allowed; "
            "  don't worry about templates.\n"
            "* NEVER include encrypted identifiers, passwords, or "
            "  anything ending in _encrypted in the reply body.\n"
        )
    else:
        how_to_reply = (
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
    return inbound_prompts.build_inbound_system_prompt(
        db,
        family_id=family_id,
        person=person,
        surface_verb=surface_verb,
        assistant_id=_assistant_id_for_family(db, family_id),
        how_to_reply=how_to_reply,
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
) -> str:
    """Drain the async agent generator and return the final reply text.

    Thin wrapper over :func:`api.ai.agent_drain.drain_agent_sync`
    that adds the SMS-flavoured fallback copy.
    """
    registry = agent_tools.build_default_registry()
    with SessionLocal() as db_for_caps:
        capabilities = agent_tools.detect_capabilities(db_for_caps, assistant_id)

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
    )

    if result.error_text and not result.final_text:
        return "Sorry — Avi here. I tried but my server hit a snag. Try again in a bit."
    return result.final_text or "Got your text — nothing to add right now."


__all__ = ["process_inbound_sms"]
