"""Sending a player their join link.

An invite goes out on every channel we can use for that person, so it
lands wherever they look first:

* **Text**, from the household's Twilio number — only to someone who
  has a mobile number on file, has confirmed the SMS opt-in
  (``people.sms_opted_in_at``) and hasn't opted out. That is the same
  rule every other text Avi starts on his own follows.
* **Email**, from the assistant's connected Gmail — to anyone with an
  email address.

When neither works the invite is marked ``failed`` with the reason, and
the host passes the link on themselves (the game screen has a share
button for exactly that).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from .. import models
from ..ai import assistants as _assistants
from ..config import Settings, get_settings
from ..db import session_scope
from ..integrations import gmail, google_oauth, twilio_sms
from ..utils.phone import normalize_phone
from . import service
from .catalog import GAME_TYPES

logger = logging.getLogger(__name__)


_SMS_TEMPLATE = (
    "{assistant} here. {inviter} invited you to play {game}! "
    "Tap to pick {pick} and join: {url}"
)
_EMAIL_SUBJECT = "{inviter} invited you to play {game}"
_EMAIL_BODY = (
    "Hi {name},\n\n"
    "{inviter} started a game of {game} and wants you in. Open this link "
    "on your phone to pick {pick} and join — there's nothing to "
    "sign in to:\n\n"
    "{url}\n\n"
    "The link is yours alone and works for the next day.\n\n"
    "— {assistant}"
)


def _twilio_ready(settings: Settings) -> bool:
    return bool(
        settings.TWILIO_ACCOUNT_SID
        and settings.TWILIO_AUTH_TOKEN
        and settings.TWILIO_PRIMARY_PHONE
    )


def sms_number(
    person: models.Person, settings: Optional[Settings] = None
) -> Optional[str]:
    """The number we may text this person an invite on, or ``None``."""
    settings = settings or get_settings()
    if not _twilio_ready(settings):
        return None
    if person.sms_opted_out or person.sms_opted_in_at is None:
        return None
    return normalize_phone(person.mobile_phone_number)


def notify_channels(
    person: models.Person, settings: Optional[Settings] = None
) -> List[str]:
    """How an invite would reach this person: any of ``'sms'``, ``'email'``."""
    channels: List[str] = []
    if sms_number(person, settings):
        channels.append("sms")
    if (person.email_address or "").strip():
        channels.append("email")
    return channels


@dataclass
class _Invite:
    """Everything needed to send one invite, read in a single transaction."""

    name: str
    inviter: str
    game: str
    pick: str
    assistant: str
    url: str
    sms_to: Optional[str]
    email_to: Optional[str]
    google_creds: Any
    problems: List[str]


def _prepare(player_id: int) -> Optional[_Invite]:
    settings = get_settings()
    with session_scope() as db:
        player = db.get(models.GamePlayer, player_id)
        if (
            player is None
            or player.invite_status != "pending"
            or player.joined_at is not None
            # Guests and the AI have no phone number or email on file.
            or player.person is None
        ):
            return None
        game = player.game
        person = player.person
        family = db.get(models.Family, game.family_id)
        assistant = (
            family.assistant.assistant_name if family and family.assistant else "Avi"
        )
        name = service.display_name(person)
        problems: List[str] = []

        sms_to = sms_number(person, settings)
        if sms_to is None and (person.mobile_phone_number or "").strip():
            if not _twilio_ready(settings):
                problems.append("Texting isn't set up")
            elif person.sms_opted_out:
                problems.append(f"{name} has opted out of texts")
            else:
                problems.append(f"{name} hasn't opted in to texts from {assistant}")

        email_to = (person.email_address or "").strip() or None
        creds = None
        if email_to:
            assistant_id = _assistants.assistant_id_for_family(db, game.family_id)
            try:
                if assistant_id is None:
                    raise google_oauth.GoogleNotConnected("no assistant")
                _row, creds = google_oauth.load_credentials(db, assistant_id)
            except google_oauth.GoogleNotConnected:
                problems.append(f"{assistant}'s Google account isn't connected")
            except (
                google_oauth.GoogleOAuthError,
                google_oauth.GoogleOAuthUnavailable,
            ) as exc:
                logger.warning("game invite: Google credentials unusable: %s", exc)
                problems.append(f"{assistant}'s Google account needs reconnecting")
        if not sms_to and not email_to and not problems:
            problems.append(f"{name} has no mobile number or email on file")

        return _Invite(
            name=name,
            inviter=service.display_name(game.created_by),
            game=GAME_TYPES[game.game_type].name,
            pick=GAME_TYPES[game.game_type].pick,
            assistant=assistant,
            url=service.play_url(player.join_token),
            sms_to=sms_to,
            email_to=email_to if creds is not None else None,
            google_creds=creds,
            problems=problems,
        )


def deliver(player_id: int) -> None:
    """Text and/or email one pending invite and record how it went.

    Never raises: a failed send is recorded on the row so the host can
    see it and share the link by hand.
    """
    invite = _prepare(player_id)
    if invite is None:
        return
    settings = get_settings()
    sent: List[str] = []
    problems = list(invite.problems)
    words = {
        "assistant": invite.assistant,
        "inviter": invite.inviter,
        "game": invite.game,
        "pick": invite.pick,
        "name": invite.name,
        "url": invite.url,
    }

    if invite.sms_to:
        try:
            twilio_sms.send_sms(
                account_sid=settings.TWILIO_ACCOUNT_SID or "",
                auth_token=settings.TWILIO_AUTH_TOKEN or "",
                from_phone=settings.TWILIO_PRIMARY_PHONE or "",
                to_phone=invite.sms_to,
                body=_SMS_TEMPLATE.format(**words),
                messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
                status_callback=settings.twilio_status_callback_url(),
            )
            sent.append("sms")
        except Exception as exc:  # noqa: BLE001 - recorded on the row instead
            logger.warning("game invite: text to player %s failed: %s", player_id, exc)
            problems.append("The text didn't go through")

    if invite.email_to:
        try:
            gmail.send_email(
                invite.google_creds,
                to=invite.email_to,
                subject=_EMAIL_SUBJECT.format(**words),
                body=_EMAIL_BODY.format(**words),
            )
            sent.append("email")
        except Exception as exc:  # noqa: BLE001 - recorded on the row instead
            logger.warning("game invite: email to player %s failed: %s", player_id, exc)
            problems.append("The email didn't go through")

    with session_scope() as db:
        player = db.get(models.GamePlayer, player_id)
        if player is None:
            return
        player.invite_status = "sent" if sent else "failed"
        player.invite_channels = ",".join(sent) or None
        player.invite_detail = "; ".join(problems) or None
    logger.info(
        "game invite: player=%s sent_via=%s problems=%s",
        player_id,
        ",".join(sent) or "-",
        "; ".join(problems) or "-",
    )
