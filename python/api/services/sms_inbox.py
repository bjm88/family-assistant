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
  household member. **Unmatched senders are silently ignored**
  (``status='ignored_unknown_sender'``) except A2P campaign keywords
  (JOIN / START / HELP), which get a canned register-or-confirm
  reply so the 10DLC campaign can pass review. That is the security
  gate — the agent loop never runs for a stranger.
* When the sender does match a registered person we open / reuse a
  ``LiveSession`` keyed on the counterparty's E.164 phone with
  ``source='sms'`` or ``source='whatsapp'`` so the two surfaces keep
  separate transcripts (different reply-length norms, different
  opt-in conventions), log the inbound into the transcript, run the
  same agent loop the live chat uses, and send the final answer back
  via Twilio's REST API (``send_sms`` / ``send_whatsapp``).
* SMS-only: if Twilio is unconfigured, the send raises, or a later
  delivery receipt comes back ``undelivered`` / ``failed`` (e.g. A2P
  30034), we email the same reply to ``Person.email_address`` when
  that member has one and the family assistant has Gmail connected.
  Instant acks stay SMS-only — only the real answer is worth the
  fallback. WhatsApp does not use this path.

Architectural parity with email
-------------------------------
Identical flow to ``services.email_inbox._handle_one_message`` —
self-loop gate → "is this an opt-out keyword?" gate → person-lookup
gate → session/transcript bookkeeping → agent loop → reply send →
audit row finalisation.

What this code DELIBERATELY does not do
---------------------------------------
* Reply to anyone whose phone number does not match (after E.164
  normalisation) one ``Person.{mobile,home,work}_phone_number`` —
  except the A2P campaign keywords (JOIN / START / YES / UNSTOP /
  HELP / INFO). Those get a canned reply even from an unknown
  number so Twilio's 10DLC reviewer can pass the program. The
  unknown-sender reply is a register-here pointer, not an agent
  conversation.
* Reply to STOP / OPTOUT / CANCEL / END / QUIT / UNSUBSCRIBE /
  REVOKE / STOPALL. Those flip ``people.sms_opted_out`` when the
  number matches a household member and never get an Avi SMS.
* Send any SMS (ack, agent reply, campaign HELP, Telegram
  verification) to a person whose ``sms_opted_out`` flag is set.
  JOIN / SUBSCRIBE clear the flag and confirm the family name.
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
from ..ai import fast_ack
from ..ai import fast_action_router
from ..ai import llm
from ..ai import session as live_session
from ..ai import tools as agent_tools
from ..ai import vision
from ..config import Settings, get_settings
from ..db import SessionLocal
from ..integrations import gmail, google_oauth, twilio_sms
from ..utils.phone import normalize_phone
from . import inbound_prompts


logger = logging.getLogger(__name__)


# Per Twilio's documentation, these single-word bodies (case-insensitive)
# trigger the carrier-level opt-out chain. We never want to send a
# reply that races Twilio's own STOP confirmation, so we treat them as
# a hard ignore.
_OPT_OUT_KEYWORDS: frozenset[str] = frozenset(
    {
        "optout",
        "opt-out",
        "cancel",
        "end",
        "quit",
        "unsubscribe",
        "revoke",
        "stop",
        "stopall",
    }
)

# A2P 10DLC campaign keywords. JOIN / SUBSCRIBE are the household's
# advertised opt-in (they also clear people.sms_opted_out). START /
# YES / UNSTOP are the carrier-standard re-subscribe aliases after a
# STOP. HELP / INFO are the required support keywords. Exact
# single-token match only — "please JOIN" still goes through the
# normal agent / unknown-sender path.
_OPT_IN_KEYWORDS: frozenset[str] = frozenset(
    {"join", "subscribe", "start", "yes", "unstop"}
)
_HELP_KEYWORDS: frozenset[str] = frozenset({"help", "info"})

_CAMPAIGN_FOOTER = (
    "Msg & data rates may apply. Reply STOP to unsubscribe, HELP for help."
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


def _outbound_extra(chan: _ChannelConfig, settings: Settings) -> dict:
    """Per-send kwargs that attach A2P + delivery-receipt plumbing.

    WhatsApp does not use A2P 10DLC, so the Messaging Service SID is
    SMS-only. Both surfaces get a status callback when the public
    webhook URL is configured so ``undelivered`` / ``30034`` lands in
    our logs instead of looking like a successful send.
    """
    extra: dict = {}
    if chan.channel == "sms" and settings.TWILIO_MESSAGING_SERVICE_SID:
        extra["messaging_service_sid"] = settings.TWILIO_MESSAGING_SERVICE_SID
    callback = settings.twilio_status_callback_url()
    if callback:
        extra["status_callback"] = callback
    return extra


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
    # STOP / OPTOUT / … flip people.sms_opted_out when we know who
    # it is. We never send a reply — Twilio's carrier handler may
    # already confirm STOP, and the household asked us not to text
    # opted-out numbers at all.
    if _is_opt_out(inbound.body):
        return _handle_opt_out(
            db,
            from_phone_bare=from_phone_bare,
            inbound_body=inbound.body,
            common=common,
        )

    # ---- A2P campaign keywords (SMS only) --------------------------
    # JOIN / SUBSCRIBE / HELP must reply even when the sender is not
    # a registered household member — Twilio's campaign reviewer
    # texts these from an unknown number and expects a compliant
    # canned message, not silence. JOIN also clears sms_opted_out.
    if chan.channel == "sms" and _is_campaign_keyword(inbound.body):
        return _handle_campaign_keyword(
            db,
            inbound=inbound,
            from_phone_bare=from_phone_bare,
            chan=chan,
            settings=settings,
            common=common,
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

    # A prior STOP/OPTOUT is stored on the person. Do not ack, do
    # not run the agent, do not send any SMS until they JOIN again.
    if chan.channel == "sms" and bool(getattr(person, "sms_opted_out", False)):
        logger.info(
            "SMS inbox: suppressing reply for opted-out person_id=%s sid=%s",
            person.person_id,
            inbound.message_sid,
        )
        return _save_audit(
            db,
            family_id=person.family_id,
            person_id=person.person_id,
            status="ignored_opted_out",
            status_reason=(
                "Person has sms_opted_out=true; no outbound or "
                "response SMS will be sent. Text JOIN to opt back in."
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
    attachments_meta, rendered_attachments, over_cap = _persist_attachments(
        db, audit, inbound
    )
    attachment_block = vision.render_attachments_for_prompt(
        rendered_attachments, extras_omitted=over_cap
    )
    if attachments_meta:
        db.commit()

    # ---- Log inbound to the transcript ------------------------------
    inbound_text = _format_inbound_for_log(
        inbound, attachments_meta, attachment_block=attachment_block
    )
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
        model=llm._model(),
    )
    audit.agent_task_id = task.agent_task_id
    db.commit()

    user_message = _format_user_message_for_agent(
        inbound, person, chan, attachment_block=attachment_block
    )

    # ---- Instant heuristic ack (two-message pattern) ---------------
    # The user's contract is: Avi confirms within milliseconds that he
    # heard them, THEN follows up with the real answer. Fire the
    # keyword-bucket ack (no LLM) on every inbound before triage so
    # SMS / WhatsApp never sit silent while the fast model classifies
    # or the 26B thinks. Chit-chat still gets a short "Working on it."
    # — two messages is the agreed UX, not a one-shot reply.
    ack_text = fast_ack.heuristic_ack(inbound.body)
    ack_message_sid: Optional[str] = None
    if ack_text and chan.sender_number and settings.AI_FAST_ACK_ENABLED:
        try:
            ack_message_sid = _send_for_channel(chan)(
                account_sid=settings.TWILIO_ACCOUNT_SID or "",
                auth_token=settings.TWILIO_AUTH_TOKEN or "",
                from_phone=chan.sender_number,
                to_phone=inbound.from_phone,
                body=ack_text,
                **_outbound_extra(chan, settings),
            )
            logger.info(
                "[orch] surface=%s instant_ack sid=%s ack_sid=%s text=%r",
                chan.label.lower(),
                inbound.message_sid,
                ack_message_sid,
                ack_text,
            )
            live_session.log_message(
                db,
                session,
                role="assistant",
                content=ack_text,
                person_id=person.person_id,
                meta={
                    "kind": f"{chan.reply_kind}_ack",
                    "channel": chan.channel,
                    "agent_task_id": task.agent_task_id,
                    "twilio_message_sid": ack_message_sid,
                    "in_reply_to": inbound.message_sid,
                },
            )
            db.commit()
        except twilio_sms.TwilioSendError as exc:
            logger.warning(
                "[orch] surface=%s instant_ack send failed sid=%s: %s "
                "(continuing with primary reply)",
                chan.label.lower(),
                inbound.message_sid,
                exc,
            )

    # ---- Unified triage shortcut -----------------------------------
    # One fast-model call decides between web-search / family-QA /
    # action-intent / heavy. Inbound attachments always escalate to
    # the heavy agent because none of the fast paths know how to
    # handle ``task_attach_message_attachment``; we pass the gate
    # via ``allow_action=False`` so the classifier can still pick
    # WEB / FAMILY_QA when appropriate.
    agent_failed = False
    final_text: Optional[str] = None
    shortcut_used: Optional[str] = None
    assistant_id_for_fast = _assistant_id_for_family(db, person.family_id)
    triage = fast_action_router.try_triage_sync(
        (inbound.body or "").strip(),
        family_id=person.family_id,
        person_id=person.person_id,
        assistant_id=assistant_id_for_fast,
        live_session_id=session.live_session_id,
        surface_label=chan.label,
        agent_task_kind=chan.agent_task_kind,
        # Attachments need the heavy agent's full tool set.
        allow_action=not bool(inbound.media),
    )
    if triage.reply:
        logger.info(
            "[orch] surface=%s path=%s sid=%s task=%s person_id=%s "
            "reply_chars=%d (skipping heavy agent)",
            chan.label.lower(),
            triage.used_path,
            inbound.message_sid,
            task.agent_task_id,
            person.person_id,
            len(triage.reply),
        )
        final_text = triage.reply
        shortcut_used = triage.used_path

    if final_text is None:
        system_prompt = _build_sms_system_prompt(
            db,
            family_id=person.family_id,
            person=person,
            from_phone=inbound.from_phone,
            chan=chan,
        )
        assistant_id_for_fam = _assistant_id_for_family(db, person.family_id)
        inbound_attachment_refs = [
            agent_tools.InboundAttachmentRef(
                media_index=m["media_index"],
                filename=m["filename"],
                mime_type=m.get("mime_type"),
                size_bytes=m.get("file_size_bytes"),
                stored_path=m["stored_path"],
                channel=chan.label.lower(),
            )
            for m in attachments_meta
        ]

        logger.info(
            "[orch] surface=%s path=heavy_agent sid=%s task=%s "
            "person_id=%s n_attachments=%d",
            chan.label.lower(),
            inbound.message_sid,
            task.agent_task_id,
            person.person_id,
            inbound.num_media,
        )

        # The contract with the user is: every inbound message
        # from a registered family member gets a reply, period.
        # Even if the agent loop crashes (LLM offline, tool
        # exception, asyncio glitch, …) we still want SOMETHING
        # to land on their phone so they know we received the
        # message and aren't silently dropping it. So: catch
        # everything here and fall back to a short, honest "I
        # tried but hit a snag" string — the audit row + the
        # agent_task row keep the full forensic trail for
        # debugging.
        try:
            final_text = _run_agent_to_completion(
                task_id=task.agent_task_id,
                family_id=person.family_id,
                assistant_id=assistant_id_for_fam,
                person_id=person.person_id,
                system_prompt=system_prompt,
                history=[],
                user_message=user_message,
                inbound_attachments=inbound_attachment_refs,
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
        if chan.channel == "sms":
            return _finish_with_email_fallback(
                db,
                audit=audit,
                session=session,
                person=person,
                inbound=inbound,
                reply_text=final_text,
                sms_error=f"{chan.sender_env_name} not configured.",
                shortcut_used=shortcut_used,
                agent_failed=agent_failed,
            )
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
            **_outbound_extra(chan, settings),
        )
    except twilio_sms.TwilioSendError as exc:
        logger.exception(
            "%s inbox: outbound send failed for sid=%s",
            chan.label,
            inbound.message_sid,
        )
        if chan.channel == "sms":
            return _finish_with_email_fallback(
                db,
                audit=audit,
                session=session,
                person=person,
                inbound=inbound,
                reply_text=final_text,
                sms_error=f"twilio send: {exc}",
                shortcut_used=shortcut_used,
                agent_failed=agent_failed,
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
            **({"shortcut": shortcut_used} if shortcut_used else {}),
            **(
                {"ack_twilio_message_sid": ack_message_sid}
                if ack_message_sid
                else {}
            ),
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


def _keyword_token(body: Optional[str]) -> str:
    """Single-token, case-folded body used for campaign keyword matching."""
    if not body:
        return ""
    return body.strip().lower()


def _is_opt_out(body: Optional[str]) -> bool:
    return _keyword_token(body) in _OPT_OUT_KEYWORDS


def _is_opt_in(body: Optional[str]) -> bool:
    return _keyword_token(body) in _OPT_IN_KEYWORDS


def _is_help(body: Optional[str]) -> bool:
    return _keyword_token(body) in _HELP_KEYWORDS


def _is_campaign_keyword(body: Optional[str]) -> bool:
    return _is_opt_in(body) or _is_help(body)


def _family_short_name(family: Optional[models.Family]) -> str:
    """'Maisano Family' → 'Maisano' so we can say 'the Maisano family'."""
    raw = (family.family_name if family else "") or ""
    name = raw.strip()
    if name.lower().endswith(" family"):
        name = name[: -len(" family")].strip()
    return name


def _register_url() -> str:
    return (get_settings().PUBLIC_BASE_URL or "http://localhost:8000").rstrip(
        "/"
    ) + "/"


def _campaign_join_body(*, family: Optional[models.Family]) -> str:
    label = _family_short_name(family)
    if label:
        return (
            f"You are registered with the {label} family. "
            f"{_CAMPAIGN_FOOTER}"
        )
    return (
        "This number isn't linked to a family yet. Register at "
        f"{_register_url()} then text JOIN again. {_CAMPAIGN_FOOTER}"
    )


def _set_sms_opt_out(person: models.Person, *, opted_out: bool) -> None:
    """Flip the per-person SMS consent flag and stamp / clear the time."""
    person.sms_opted_out = opted_out
    person.sms_opted_out_at = _utcnow() if opted_out else None


def _handle_opt_out(
    db: Session,
    *,
    from_phone_bare: str,
    inbound_body: Optional[str],
    common: dict,
) -> models.SmsInboxMessage:
    """Record a STOP-style keyword. Never send a reply."""
    person = _lookup_family_member_by_phone(db, from_phone_bare)
    token = _keyword_token(inbound_body)
    if person is not None:
        _set_sms_opt_out(person, opted_out=True)
        db.commit()
        logger.info(
            "SMS inbox: person_id=%s opted out via %r",
            person.person_id,
            token,
        )
        return _save_audit(
            db,
            family_id=person.family_id,
            person_id=person.person_id,
            status="ignored_stop",
            status_reason=(
                f"Opt-out keyword {token!r}; sms_opted_out set. "
                "No SMS reply sent."
            ),
            **common,
        )
    return _save_audit(
        db,
        family_id=None,
        person_id=None,
        status="ignored_stop",
        status_reason=(
            f"Opt-out keyword {token!r} from an unknown number; "
            "Twilio handles the carrier-level opt-out."
        ),
        **common,
    )


def _campaign_help_body(*, family: Optional[models.Family]) -> str:
    site = _register_url()
    label = _family_short_name(family)
    who = (
        f"Avi is the {label} family's AI assistant"
        if label
        else "Avi is your Family AI Assistant"
    )
    return (
        f"{who} — a private household helper that answers questions, "
        "keeps the calendar and tasks, and sends reminders over text. "
        f"Learn more: {site} {_CAMPAIGN_FOOTER}"
    )


def _handle_campaign_keyword(
    db: Session,
    *,
    inbound: twilio_sms.InboundSms,
    from_phone_bare: str,
    chan: _ChannelConfig,
    settings: Settings,
    common: dict,
) -> models.SmsInboxMessage:
    """Canned JOIN / HELP reply for A2P 10DLC campaign compliance.

    Looks the sender up the same way as a normal inbound so a linked
    phone gets the family name; an unknown phone gets the public
    register URL. Never opens a session or spends an LLM call.
    """
    person = _lookup_family_member_by_phone(db, from_phone_bare)
    family = (
        db.get(models.Family, person.family_id) if person is not None else None
    )
    if _is_help(inbound.body):
        if person is not None and bool(getattr(person, "sms_opted_out", False)):
            return _save_audit(
                db,
                family_id=person.family_id,
                person_id=person.person_id,
                status="ignored_opted_out",
                status_reason=(
                    "HELP received but person has sms_opted_out=true; "
                    "no SMS sent. Text JOIN to opt back in."
                ),
                **common,
            )
        reply = _campaign_help_body(family=family)
        kind = "help"
    else:
        if person is not None:
            _set_sms_opt_out(person, opted_out=False)
            db.commit()
        reply = _campaign_join_body(family=family)
        kind = "join"

    if not chan.sender_number:
        return _save_audit(
            db,
            family_id=person.family_id if person else None,
            person_id=person.person_id if person else None,
            status="failed",
            status_reason=(
                f"Campaign {kind} keyword received but "
                f"{chan.sender_env_name} is not configured."
            ),
            **common,
        )

    send_func = _send_for_channel(chan)
    try:
        reply_sid = send_func(
            account_sid=settings.TWILIO_ACCOUNT_SID or "",
            auth_token=settings.TWILIO_AUTH_TOKEN or "",
            from_phone=chan.sender_number,
            to_phone=inbound.from_phone,
            body=reply,
            **_outbound_extra(chan, settings),
        )
    except twilio_sms.TwilioSendError as exc:
        logger.exception(
            "SMS inbox: campaign %s reply failed for sid=%s",
            kind,
            inbound.message_sid,
        )
        return _save_audit(
            db,
            family_id=person.family_id if person else None,
            person_id=person.person_id if person else None,
            status="failed",
            status_reason=f"campaign {kind} twilio send: {exc}",
            **common,
        )

    logger.info(
        "SMS inbox: campaign %s reply sent to %s family=%s sid=%s",
        kind,
        inbound.from_phone,
        family.family_name if family else None,
        reply_sid,
    )
    return _save_audit(
        db,
        family_id=person.family_id if person else None,
        person_id=person.person_id if person else None,
        status="processed_replied",
        status_reason=(
            f"A2P campaign {kind} keyword; canned reply "
            f"(family={'linked' if family else 'unlinked'})."
        ),
        reply_message_sid=reply_sid,
        **common,
    )


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
) -> tuple[List[dict], List[vision.RenderableAttachment], int]:
    """Download MMS media, persist rows, and run the vision pipeline.

    Returns a 3-tuple of:

    1. ``meta`` — small ``{index, mime_type, stored_path, file_size_bytes}``
       dicts for the transcript ``meta`` blob.
    2. ``rendered`` — :class:`vision.RenderableAttachment` per
       attachment we analysed (in order), suitable for
       :func:`vision.render_attachments_for_prompt`.
    3. ``over_cap`` — count of attachments stored on disk but not
       analysed because we hit ``AI_ATTACHMENT_MAX_PER_MESSAGE``.

    Failures on a single media file don't block the others — we log
    and keep going so a busted CDN URL doesn't drop the whole reply.
    """
    if not inbound.media or audit.family_id is None:
        return [], [], 0
    settings = get_settings()
    meta: List[dict] = []
    inputs: List[vision.AttachmentInput] = []
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
        # Synthetic filename — Twilio doesn't surface the original.
        synthetic_name = f"attachment-{index}{twilio_sms.extension_for_mime(blob.mime_type) or ''}"
        meta.append(
            {
                "media_index": index,
                "mime_type": blob.mime_type,
                "stored_path": rel_path,
                "file_size_bytes": size,
                "filename": synthetic_name,
            }
        )
        inputs.append(
            vision.AttachmentInput(
                index=index,
                path=settings.storage_root / rel_path,
                filename=synthetic_name,
                mime_type=blob.mime_type,
                size_bytes=size,
            )
        )
    if meta:
        db.flush()
    rendered, over_cap = vision.describe_many(
        inputs, max_to_describe=settings.AI_ATTACHMENT_MAX_PER_MESSAGE
    )
    return meta, rendered, over_cap


def _format_inbound_for_log(
    inbound: twilio_sms.InboundSms,
    attachments: List[dict],
    *,
    attachment_block: str = "",
) -> str:
    """Render an inbound SMS so it reads naturally in the history view.

    ``attachment_block`` is the rendered, captioned list from the
    vision pipeline; falls back to a one-line ``[attachment N: …]``
    summary built from the persisted metadata when no captions are
    available (e.g. ``AI_VISION_ENABLED=false``), so the transcript
    is never silent about media.
    """
    lines = [
        f"From: {inbound.from_phone}",
        f"To: {inbound.to_phone}",
        "",
        inbound.body or "(no body)",
    ]
    if attachment_block:
        lines.append("")
        lines.append(attachment_block)
    elif attachments:
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
    *,
    attachment_block: str = "",
) -> str:
    """Wrap the inbound so the agent knows what surface this came through.

    Tagging the surface in the user-message header lets the agent
    instinctively right-size its reply (a one-line SMS reply vs. a
    chat-style WhatsApp reply) without us having to pile every nuance
    into the system prompt. The optional ``attachment_block`` is the
    captioned list from the vision pipeline, appended verbatim so the
    text-only Gemma model can reason about images and PDFs.
    """
    name = person.preferred_name or person.first_name or inbound.from_phone
    media_hint = ""
    if inbound.num_media:
        media_hint = (
            f" (with {inbound.num_media} attached "
            f"{'image' if inbound.num_media == 1 else 'images'})"
        )
    surface = "Text message" if chan.channel == "sms" else "WhatsApp message"
    base = (
        f"[{surface} from {name} <{inbound.from_phone}>{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no body)'}"
    )
    if attachment_block:
        return f"{base}\n\n{attachment_block}"
    return base


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
    inbound_attachments: Optional[List[agent_tools.InboundAttachmentRef]] = None,
) -> str:
    """Drain the async agent generator and return the final reply text.

    Thin wrapper over :func:`api.ai.agent_drain.drain_agent_sync`
    that adds the SMS-flavoured fallback copy.
    """
    registry = agent_tools.build_default_registry()
    with SessionLocal() as db_for_caps:
        capabilities = agent_tools.detect_capabilities(db_for_caps, assistant_id)

    # Synthetic capability that gates the ``task_attach_message_attachment``
    # tool — present only when this inbound actually has attachments.
    # Trimming that one tool's schema off text-only turns shaves ~1 KB
    # off the system prompt and removes one decoy from the model's
    # tool-pick menu (it can't claim to attach something we couldn't
    # have given it).
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
        },
    )

    if result.error_text and not result.final_text:
        return "Sorry — Avi here. I tried but my server hit a snag. Try again in a bit."
    return result.final_text or "Got your text — nothing to add right now."


# ---------------------------------------------------------------------------
# Email fallback when SMS cannot be delivered
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EmailFallbackResult:
    sent: bool
    detail: str
    gmail_message_id: Optional[str] = None


def _format_sms_fallback_email(*, inbound_body: Optional[str], reply_text: str) -> Tuple[str, str]:
    """Subject + body for the SMS→email fallback."""
    subject = "Avi couldn't deliver a text — here's the reply"
    quoted = (inbound_body or "").strip() or "(no text)"
    body = (
        "Avi here. I received your text but couldn't deliver the reply "
        "to your phone, so I'm sending it by email instead.\n\n"
        f"You wrote:\n{quoted}\n\n"
        f"Avi's reply:\n{reply_text}"
    )
    return subject, body


def _email_sms_reply(
    db: Session,
    *,
    person: models.Person,
    inbound_body: Optional[str],
    reply_text: str,
) -> _EmailFallbackResult:
    """Send ``reply_text`` to ``person.email_address`` via the family Gmail.

    Returns a result even when we skip (no email, no Google grant) so
    callers can put the reason on the audit row. Never raises.
    """
    email = (person.email_address or "").strip()
    if not email:
        return _EmailFallbackResult(False, "person has no email_address")
    assistant_id = _assistant_id_for_family(db, person.family_id)
    if assistant_id is None:
        return _EmailFallbackResult(False, "no assistant for family")
    try:
        _row, creds = google_oauth.load_credentials(db, assistant_id)
    except google_oauth.GoogleNotConnected:
        return _EmailFallbackResult(False, "google not connected")
    except (google_oauth.GoogleOAuthError, google_oauth.GoogleOAuthUnavailable) as exc:
        return _EmailFallbackResult(False, f"google oauth: {exc}")
    subject, body = _format_sms_fallback_email(
        inbound_body=inbound_body, reply_text=reply_text
    )
    try:
        gmail_id = gmail.send_email(creds, to=email, subject=subject, body=body)
    except gmail.GmailSendError as exc:
        logger.exception(
            "SMS email fallback: Gmail send failed person_id=%s to=%s",
            person.person_id,
            email,
        )
        return _EmailFallbackResult(False, f"gmail send: {exc}")
    logger.info(
        "[orch] surface=sms email_fallback person_id=%s to=%s gmail_id=%s",
        person.person_id,
        email,
        gmail_id,
    )
    return _EmailFallbackResult(True, f"emailed fallback to {email}", gmail_id)


def _finish_with_email_fallback(
    db: Session,
    *,
    audit: models.SmsInboxMessage,
    session: models.LiveSession,
    person: models.Person,
    inbound: twilio_sms.InboundSms,
    reply_text: str,
    sms_error: str,
    shortcut_used: Optional[str],
    agent_failed: bool,
) -> models.SmsInboxMessage:
    """Record a Twilio miss and, when possible, deliver the reply by email."""
    fallback = _email_sms_reply(
        db,
        person=person,
        inbound_body=inbound.body,
        reply_text=reply_text,
    )
    live_session.log_message(
        db,
        session,
        role="assistant",
        content=reply_text,
        person_id=person.person_id,
        meta={
            "kind": "sms_email_fallback" if fallback.sent else "sms_reply",
            "channel": "sms",
            "in_reply_to": inbound.message_sid,
            "sms_error": sms_error,
            **({"shortcut": shortcut_used} if shortcut_used else {}),
            **(
                {"gmail_message_id": fallback.gmail_message_id}
                if fallback.gmail_message_id
                else {}
            ),
        },
    )
    if fallback.sent:
        audit.status = "processed_replied"
        extra = " Agent loop crashed; emailed fallback apology." if agent_failed else ""
        audit.status_reason = f"{sms_error}; {fallback.detail}.{extra}".rstrip()
    else:
        audit.status = "failed"
        audit.status_reason = f"{sms_error}; email fallback: {fallback.detail}"
    db.commit()
    return audit


def _reply_body_from_session(
    db: Session, audit: models.SmsInboxMessage, twilio_sid: str
) -> Optional[str]:
    """Recover the outbound SMS body we logged for ``twilio_sid``."""
    if audit.live_session_id is None:
        return None
    rows = (
        db.execute(
            select(models.LiveSessionMessage)
            .where(
                models.LiveSessionMessage.live_session_id == audit.live_session_id
            )
            .where(models.LiveSessionMessage.role == "assistant")
            .order_by(models.LiveSessionMessage.live_session_message_id.desc())
            .limit(12)
        )
        .scalars()
        .all()
    )
    sid_match: Optional[str] = None
    kind_match: Optional[str] = None
    for row in rows:
        meta = row.meta or {}
        if meta.get("kind") == "sms_email_fallback":
            # Already fell back — caller uses this as an idempotency signal
            # by seeing status_reason, but skip using this as the SMS body.
            continue
        if meta.get("twilio_message_sid") == twilio_sid:
            sid_match = row.content
            break
        if kind_match is None and meta.get("kind") == "sms_reply":
            kind_match = row.content
    return sid_match or kind_match


def handle_outbound_delivery_failure(
    db: Session,
    *,
    twilio_message_sid: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Email the SMS reply if a later Twilio receipt says it never landed.

    Instant-ack SIDs are ignored (they are not stored on
    ``reply_message_sid``). Already-emailed rows are a no-op so Twilio
    retries do not double-send. WhatsApp receipts are ignored.
    """
    if not twilio_message_sid:
        return
    audit = db.execute(
        select(models.SmsInboxMessage).where(
            models.SmsInboxMessage.reply_message_sid == twilio_message_sid
        )
    ).scalar_one_or_none()
    if audit is None or audit.channel != "sms":
        return
    if audit.status_reason and "emailed fallback" in audit.status_reason:
        logger.info(
            "SMS delivery failure already emailed sid=%s; skipping.",
            twilio_message_sid,
        )
        return
    person = audit.person or (
        db.get(models.Person, audit.person_id) if audit.person_id else None
    )
    if person is None:
        logger.warning(
            "SMS delivery failure sid=%s has no person; cannot email.",
            twilio_message_sid,
        )
        return
    reply_text = _reply_body_from_session(db, audit, twilio_message_sid)
    if not reply_text:
        logger.warning(
            "SMS delivery failure sid=%s: no reply body on the session.",
            twilio_message_sid,
        )
        return
    err = f"SMS undelivered ({error_code or 'unknown'})"
    if error_message:
        err = f"{err}: {error_message}"
    fallback = _email_sms_reply(
        db,
        person=person,
        inbound_body=audit.body,
        reply_text=reply_text,
    )
    session = (
        db.get(models.LiveSession, audit.live_session_id)
        if audit.live_session_id
        else None
    )
    if session is not None:
        live_session.log_message(
            db,
            session,
            role="assistant",
            content=reply_text,
            person_id=person.person_id,
            meta={
                "kind": "sms_email_fallback" if fallback.sent else "sms_reply",
                "channel": "sms",
                "in_reply_to": audit.twilio_message_sid,
                "failed_twilio_message_sid": twilio_message_sid,
                "sms_error": err,
                **(
                    {"gmail_message_id": fallback.gmail_message_id}
                    if fallback.gmail_message_id
                    else {}
                ),
            },
        )
    if fallback.sent:
        audit.status = "processed_replied"
        audit.status_reason = f"{err}; {fallback.detail}"
    else:
        audit.status = "failed"
        audit.status_reason = f"{err}; email fallback: {fallback.detail}"
    db.commit()


__all__ = ["handle_outbound_delivery_failure", "process_inbound_sms"]
