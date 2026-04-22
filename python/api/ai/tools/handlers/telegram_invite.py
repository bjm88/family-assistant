"""``telegram_invite`` — onboard a household member onto the bot.

Telegram bots cannot send the first message in a conversation — that
rule is enforced server-side by Telegram, so we cannot just look up
a household member's Telegram handle and message them out of the
blue. The work-around is a deep-link invite:

1. Mint a one-time URL-safe token and persist it on a
   ``telegram_invites`` row pointing at the invitee's Person.
2. Build the URL ``https://t.me/<bot_username>?start=<token>``
   using the cached ``getMe`` lookup.
3. Deliver the URL to the invitee through a channel we already
   own — preferring SMS when they have a ``mobile_phone_number``
   on file, falling back to email via the assistant's connected
   Gmail when they have an ``email_address``.
4. When the invitee taps the link, Telegram opens the bot with a
   "Start" button pre-filled. Tapping Start delivers
   ``/start <token>`` to the bot — exactly what
   ``services.telegram_inbox._claim_telegram_invite`` consumes
   to bind ``people.telegram_user_id`` and reply with a welcome.

Authz: requires an identified speaker (so we have someone to
attribute ``created_by_person_id`` to) and the invitee must belong
to the same household. The household privacy matrix that gates
secret-reveal tools doesn't apply here — onboarding a sibling /
spouse / parent to Telegram is a routine household task, not a
privileged data read.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

from .... import models
from ....config import get_settings
from ....integrations import (
    google_oauth,
    telegram as telegram_api,
    twilio_sms,
)
from ....integrations.gmail import GmailSendError, send_email
from .._registry import ToolContext, ToolError, ToolResult


TELEGRAM_INVITE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the household member to invite. Use "
                "lookup_person first if you only have a name."
            ),
        },
        "channel": {
            "type": "string",
            "enum": ["auto", "sms", "email"],
            "description": (
                "How to deliver the invite link. 'auto' (default) "
                "prefers SMS when the person has a mobile phone on "
                "file, else email. Force one explicitly when the "
                "user asks 'text it to her' / 'email it to him'."
            ),
        },
    },
    "required": ["person_id"],
}


_SMS_TEMPLATE = (
    "{assistant_name} here. Tap to start chatting with me on "
    "Telegram — your messages will reach me just like a phone "
    "call or email: {url}"
)

_EMAIL_SUBJECT_TEMPLATE = "Chat with {assistant_name} on Telegram"

_EMAIL_BODY_TEMPLATE = (
    "Hi {invitee_name},\n\n"
    "{assistant_name} here. Tap the link below in Telegram to "
    "start chatting with me — your messages from then on will "
    "reach me directly, just like the family chat or an email:\n\n"
    "{url}\n\n"
    "If you don't have Telegram installed yet, you can grab it "
    "free from the App Store / Play Store first. The link expires "
    "on {expires_human}.\n\n"
    "— {assistant_name}"
)


async def handle_telegram_invite(
    ctx: ToolContext,
    person_id: int,
    channel: str = "auto",
) -> ToolResult:
    settings = get_settings()
    if ctx.person_id is None:
        raise ToolError(
            "I can't issue a Telegram invite without first knowing "
            "who is asking. Greet me on camera (or message me from "
            "a registered email / phone / Telegram account) and try "
            "again."
        )
    if channel not in ("auto", "sms", "email"):
        raise ToolError(
            f"channel must be one of 'auto', 'sms', 'email'; got {channel!r}."
        )
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ToolError(
            "Telegram bot token isn't configured on this server "
            "(TELEGRAM_BOT_TOKEN missing) — I can't generate an "
            "invite link."
        )

    invitee = ctx.db.get(models.Person, int(person_id))
    if invitee is None or invitee.family_id != ctx.family_id:
        raise ToolError(
            f"person_id={person_id} is not a member of this family."
        )

    if invitee.telegram_user_id is not None:
        # No need for an invite — they're already linked. Surface a
        # clean error instead of silently churning a token the user
        # would never use.
        return ToolResult(
            ok=True,
            output={
                "already_linked": True,
                "person_id": invitee.person_id,
                "telegram_user_id": invitee.telegram_user_id,
                "telegram_username": invitee.telegram_username,
                "message": (
                    f"{invitee.preferred_name or invitee.first_name} "
                    "is already connected to me on Telegram — no "
                    "invite needed."
                ),
            },
            summary=(
                f"{invitee.preferred_name or invitee.first_name} is "
                "already on Telegram with me."
            ),
        )

    # Resolve the bot username for the deep link. Cached so repeated
    # calls (or many invites in quick succession) don't hammer the
    # Bot API.
    try:
        identity = await asyncio.to_thread(
            telegram_api.get_me_cached, settings.TELEGRAM_BOT_TOKEN
        )
    except telegram_api.TelegramReadError as exc:
        raise ToolError(f"Couldn't reach Telegram to mint invite: {exc}") from exc
    if not identity.username:
        raise ToolError(
            "Bot has no @username — open @BotFather and assign one, "
            "otherwise the invite URL can't be built."
        )

    chosen_channel, sent_to = _resolve_invite_channel(
        invitee=invitee, channel=channel
    )

    invite, reused = _find_or_mint_invite(
        ctx.db,
        invitee=invitee,
        created_by_person_id=ctx.person_id,
        channel=chosen_channel,
        sent_to=sent_to,
    )
    invite_url = telegram_api.build_invite_url(
        bot_username=identity.username,
        payload_token=invite.payload_token,
    )

    family = ctx.db.get(models.Family, ctx.family_id)
    assistant_name = (
        family.assistant.assistant_name
        if family and family.assistant
        else "Avi"
    )
    invitee_name = (
        invitee.preferred_name
        or invitee.first_name
        or "there"
    )
    expires_human = invite.expires_at.strftime("%A %B %-d, %Y")

    if chosen_channel == "sms":
        body = _SMS_TEMPLATE.format(
            assistant_name=assistant_name,
            url=invite_url,
        )
        sid = await _send_invite_sms(
            settings=settings, to_phone=sent_to, body=body
        )
        delivery = {
            "sms_message_sid": sid,
            "sent_to_phone": sent_to,
        }
    else:
        subject = _EMAIL_SUBJECT_TEMPLATE.format(
            assistant_name=assistant_name
        )
        body = _EMAIL_BODY_TEMPLATE.format(
            invitee_name=invitee_name,
            assistant_name=assistant_name,
            url=invite_url,
            expires_human=expires_human,
        )
        message_id = await _send_invite_email(
            ctx=ctx, to=sent_to, subject=subject, body=body
        )
        delivery = {
            "gmail_message_id": message_id,
            "sent_to_email": sent_to,
            "subject": subject,
        }

    ctx.db.commit()

    return ToolResult(
        ok=True,
        output={
            "telegram_invite_id": invite.telegram_invite_id,
            "person_id": invitee.person_id,
            "channel": chosen_channel,
            "sent_to": sent_to,
            "invite_url": invite_url,
            "expires_at": invite.expires_at.isoformat(),
            "reused_outstanding_invite": reused,
            "delivery": delivery,
        },
        summary=(
            f"Sent {invitee_name} a Telegram invite via "
            f"{chosen_channel} ({sent_to})."
            + (" (reused outstanding invite)" if reused else "")
        ),
    )


def _resolve_invite_channel(
    *, invitee: models.Person, channel: str
) -> tuple[str, str]:
    """Pick the delivery channel and return ``(channel, destination)``."""
    has_phone = bool((invitee.mobile_phone_number or "").strip())
    has_email = bool((invitee.email_address or "").strip())

    if channel == "sms":
        if not has_phone:
            raise ToolError(
                f"{invitee.preferred_name or invitee.first_name} has "
                "no mobile_phone_number on file, so I can't text them "
                "the invite. Try channel='email' or add a phone in "
                "the admin console first."
            )
        return "sms", invitee.mobile_phone_number.strip()

    if channel == "email":
        if not has_email:
            raise ToolError(
                f"{invitee.preferred_name or invitee.first_name} has "
                "no email_address on file, so I can't email them the "
                "invite. Try channel='sms' or add an email in the "
                "admin console first."
            )
        return "email", invitee.email_address.strip()

    if has_phone:
        return "sms", invitee.mobile_phone_number.strip()
    if has_email:
        return "email", invitee.email_address.strip()
    raise ToolError(
        f"{invitee.preferred_name or invitee.first_name} has neither "
        "a mobile phone nor an email on file, so there's no way for "
        "me to deliver a Telegram invite. Add one to their profile "
        "in the admin console and try again."
    )


def _find_or_mint_invite(
    db: Session,
    *,
    invitee: models.Person,
    created_by_person_id: int,
    channel: str,
    sent_to: str,
) -> tuple[models.TelegramInvite, bool]:
    """Reuse the active invite row for this person, or mint a fresh one.

    The partial-unique index ``uq_telegram_invites_active_per_person``
    enforces at most one ``(claimed_at IS NULL AND revoked_at IS
    NULL)`` row per person, so we always find at most one to reuse.
    If an existing row is still within its TTL we keep its token
    (so an old SMS the recipient might still have on their phone
    keeps working) and just update the audit fields. If it expired,
    we refresh ``expires_at`` to a new 30-day window — same effect:
    the previously-delivered link starts working again. Returns
    ``(invite, reused)`` so the model can phrase the reply
    accurately ("here's the link I sent earlier" vs "here's a fresh
    link").
    """
    from sqlalchemy import select as _select

    now = datetime.now(timezone.utc)
    active = db.execute(
        _select(models.TelegramInvite)
        .where(models.TelegramInvite.person_id == invitee.person_id)
        .where(models.TelegramInvite.claimed_at.is_(None))
        .where(models.TelegramInvite.revoked_at.is_(None))
        .order_by(models.TelegramInvite.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        active.sent_via = channel
        active.sent_to = sent_to
        if active.expires_at <= now:
            # Token had aged out — extend its life rather than
            # rotating, so the URL the household admin sent earlier
            # is still tappable.
            active.expires_at = now + models.TELEGRAM_INVITE_DEFAULT_TTL
        db.flush()
        return active, True

    invite = models.TelegramInvite(
        family_id=invitee.family_id,
        person_id=invitee.person_id,
        created_by_person_id=created_by_person_id,
        payload_token=models.generate_invite_token(),
        sent_via=channel,
        sent_to=sent_to,
        expires_at=now + models.TELEGRAM_INVITE_DEFAULT_TTL,
    )
    db.add(invite)
    db.flush()
    return invite, False


async def _send_invite_sms(*, settings, to_phone: str, body: str) -> str:
    if not (
        settings.TWILIO_ACCOUNT_SID
        and settings.TWILIO_AUTH_TOKEN
        and settings.TWILIO_PRIMARY_PHONE
    ):
        raise ToolError(
            "Twilio isn't configured (need TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, TWILIO_PRIMARY_PHONE) so I can't "
            "text the invite. Try channel='email' instead."
        )
    try:
        return await asyncio.to_thread(
            twilio_sms.send_sms,
            account_sid=settings.TWILIO_ACCOUNT_SID,
            auth_token=settings.TWILIO_AUTH_TOKEN,
            from_phone=settings.TWILIO_PRIMARY_PHONE,
            to_phone=to_phone,
            body=body,
        )
    except twilio_sms.TwilioSendError as exc:
        raise ToolError(f"Twilio refused the invite SMS: {exc}") from exc


async def _send_invite_email(
    *, ctx: ToolContext, to: str, subject: str, body: str
) -> str:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — I can't "
            "send the invite by email."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as exc:
        raise ToolError(
            f"Google isn't connected for this assistant ({exc}) — "
            "try channel='sms' instead."
        ) from exc
    except google_oauth.GoogleOAuthError as exc:
        raise ToolError(f"Google auth error: {exc}") from exc
    try:
        return await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as exc:
        raise ToolError(f"Gmail refused the invite email: {exc}") from exc
