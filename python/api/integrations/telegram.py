"""Telegram Bot API adapter.

Three jobs, all small enough to live in one file:

1. **Long-poll** the Bot API for new ``update`` objects via ``getUpdates``.
   We use polling rather than webhooks so the household app works on
   any laptop / Mac Studio without a public URL — the only secret the
   operator has to configure is ``TELEGRAM_BOT_TOKEN``.
2. **Send** an outbound reply via ``sendMessage``. Plain ``httpx`` (no
   python-telegram-bot dependency) — the Bot API is just JSON over
   HTTPS and pulling in the SDK would balloon our import time.
3. **Download** an attachment via ``getFile`` + the file-server URL.
   The file-server URL stops working ~60 minutes after delivery so we
   always copy attachments to local storage immediately.

Nothing in this module looks at the database — it's pure adapter code.
The orchestration (dedup, person lookup, agent loop, reply) lives in
:mod:`api.services.telegram_inbox`.

Bot API reference: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import logging
import mimetypes
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)


class TelegramSendError(RuntimeError):
    """Raised when the Bot API rejects an outbound call."""


class TelegramReadError(RuntimeError):
    """Raised when getUpdates / getFile / file-download fails."""


_TELEGRAM_API_BASE = "https://api.telegram.org"


def _bot_endpoint(bot_token: str, method: str) -> str:
    return f"{_TELEGRAM_API_BASE}/bot{bot_token}/{method}"


def _file_endpoint(bot_token: str, file_path: str) -> str:
    return f"{_TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"


# ---------------------------------------------------------------------------
# Long-poll: getUpdates
# ---------------------------------------------------------------------------


def get_updates(
    *,
    bot_token: str,
    offset: Optional[int] = None,
    timeout_seconds: int = 25,
    limit: int = 25,
) -> List[Mapping[str, Any]]:
    """Long-poll the Bot API and return the raw ``result`` array.

    Parameters
    ----------
    offset
        ``update_id`` of the first update to return. Pass
        ``last_seen_update_id + 1`` to acknowledge everything we've
        already processed — Telegram drops acknowledged updates from
        the queue after a ``getUpdates`` with a higher ``offset``.
    timeout_seconds
        How long Telegram will hold the request open if there's
        nothing new. 25 s keeps us well below httpx's default 30 s
        client timeout.
    limit
        Max updates returned per call. The poller caps further by its
        own ``AI_TELEGRAM_INBOX_MAX_PER_TICK``.
    """
    if not bot_token:
        raise TelegramReadError(
            "TELEGRAM_BOT_TOKEN missing — set it in .env."
        )
    payload: dict[str, Any] = {
        "timeout": int(timeout_seconds),
        "limit": int(limit),
        # We deliberately do NOT subscribe to channel posts /
        # callback queries / inline queries — we only act on direct
        # messages addressed to the bot.
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = int(offset)

    # Give httpx a slightly larger budget than Telegram's long-poll
    # window so the natural "nothing new for 25 s" case never raises.
    http_timeout = float(timeout_seconds) + 10.0

    try:
        resp = httpx.post(
            _bot_endpoint(bot_token, "getUpdates"),
            json=payload,
            timeout=http_timeout,
        )
    except httpx.HTTPError as exc:
        raise TelegramReadError(f"transport error: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("description") or resp.text[:300]
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise TelegramReadError(f"HTTP {resp.status_code}: {detail}")

    body = resp.json()
    if not body.get("ok"):
        raise TelegramReadError(
            f"Bot API returned ok=false: {body.get('description')!r}"
        )
    return list(body.get("result") or [])


# ---------------------------------------------------------------------------
# Identity: getMe
# ---------------------------------------------------------------------------


@dataclass
class BotIdentity:
    """Subset of ``getMe`` we actually care about."""

    user_id: int
    username: Optional[str]
    first_name: Optional[str]


# Module-level cache for ``get_me_cached``. Keyed on bot_token so a
# token rotation invalidates automatically. The TTL is generous (24 h)
# because a bot's username and id never change for the life of the
# token; the only reason to re-fetch at all is paranoia about a stale
# cache surviving a token swap during long-running tests.
_BOT_IDENTITY_CACHE: dict[str, Tuple[BotIdentity, float]] = {}
_BOT_IDENTITY_LOCK = threading.Lock()
_BOT_IDENTITY_TTL_SECONDS = 24 * 60 * 60


def get_me_cached(bot_token: str) -> BotIdentity:
    """Cached wrapper around :func:`get_me`.

    Used by callers (notably the agent's ``telegram_invite`` tool)
    that need the bot's ``@username`` to assemble a deep-link URL but
    don't want to hit Telegram on every invocation. The first call
    pays the network round-trip; subsequent calls inside the TTL are
    essentially free.
    """
    if not bot_token:
        raise TelegramReadError("TELEGRAM_BOT_TOKEN missing.")
    now = time.monotonic()
    with _BOT_IDENTITY_LOCK:
        cached = _BOT_IDENTITY_CACHE.get(bot_token)
        if cached is not None and now - cached[1] < _BOT_IDENTITY_TTL_SECONDS:
            return cached[0]
    fresh = get_me(bot_token)
    with _BOT_IDENTITY_LOCK:
        _BOT_IDENTITY_CACHE[bot_token] = (fresh, now)
    return fresh


def build_invite_url(*, bot_username: str, payload_token: str) -> str:
    """Assemble a ``t.me/<bot>?start=<token>`` deep-link URL.

    Telegram opens this in the user's installed Telegram app and
    pre-fills a "Start" button which, when tapped, delivers
    ``/start <payload_token>`` to the bot — exactly the input
    :func:`api.services.telegram_inbox` expects in order to claim
    the matching ``telegram_invites`` row.
    """
    handle = (bot_username or "").lstrip("@").strip()
    if not handle:
        raise TelegramReadError(
            "Bot has no @username — set one in @BotFather before "
            "issuing invites."
        )
    if not payload_token:
        raise TelegramReadError("payload_token must be non-empty.")
    return f"https://t.me/{handle}?start={payload_token}"


def build_request_contact_keyboard(
    button_label: str = "Share my phone number",
) -> dict[str, Any]:
    """Return a ``reply_markup`` dict that prompts the user for a contact share.

    Telegram renders this as a one-button reply keyboard at the
    bottom of the chat. Tapping it pops Telegram's native consent
    dialog ("Share your phone number with @YourBot?"); on confirm the
    bot receives a follow-up message whose ``message.contact`` field
    carries ``phone_number``, ``first_name``, ``last_name`` and the
    sender's own ``user_id`` — exactly what the inbox auto-link path
    needs to bind the sender to a Person row.

    We mark the keyboard ``one_time_keyboard=true`` so Telegram hides
    it after the first use (keeps the chat UI clean), and
    ``selective=true`` is intentionally NOT set: we want every
    participant in a group to be able to share if needed.

    The bot can NOT read a user's phone number or email through any
    other Bot API surface — there's no ``getChatMember`` field, no
    ``getUser`` endpoint, nothing. Consent via this button is the one
    and only path.
    """
    return {
        "keyboard": [
            [
                {
                    "text": button_label,
                    "request_contact": True,
                }
            ]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard_markup() -> dict[str, Any]:
    """Return the ``reply_markup`` payload that clears a custom keyboard.

    Send this with the welcome message right after a successful
    contact-share auto-link so the user's normal text input UI comes
    back instead of a stale "Share my phone number" button.
    """
    return {"remove_keyboard": True}


def get_me(bot_token: str, *, timeout_seconds: float = 10.0) -> BotIdentity:
    """Return our own bot's identity so the inbox can detect self-loops."""
    if not bot_token:
        raise TelegramReadError("TELEGRAM_BOT_TOKEN missing.")
    try:
        resp = httpx.post(
            _bot_endpoint(bot_token, "getMe"),
            json={},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TelegramReadError(f"transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise TelegramReadError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    if not body.get("ok"):
        raise TelegramReadError(
            f"getMe returned ok=false: {body.get('description')!r}"
        )
    result = body.get("result") or {}
    return BotIdentity(
        user_id=int(result.get("id") or 0),
        username=result.get("username"),
        first_name=result.get("first_name"),
    )


# ---------------------------------------------------------------------------
# Outbound: sendMessage
# ---------------------------------------------------------------------------


def send_message(
    *,
    bot_token: str,
    chat_id: int,
    text: str,
    reply_to_message_id: Optional[int] = None,
    reply_markup: Optional[Mapping[str, Any]] = None,
    timeout_seconds: float = 15.0,
) -> int:
    """Send a plain-text message and return Telegram's message_id.

    We deliberately omit ``parse_mode`` so any stray ``*`` / ``_`` /
    ``[`` characters in the agent's reply land verbatim instead of
    triggering a Markdown / HTML parse error and having the whole
    message rejected.

    ``reply_markup`` is forwarded verbatim to the Bot API. Pass the
    output of :func:`build_request_contact_keyboard` to surface a
    one-tap "share my phone number" prompt, or
    :func:`remove_keyboard_markup` to clear a previously-shown
    keyboard. ``None`` (the default) leaves the user's input UI
    untouched.
    """
    if not bot_token:
        raise TelegramSendError("TELEGRAM_BOT_TOKEN missing.")
    payload: dict[str, Any] = {
        "chat_id": int(chat_id),
        "text": text,
        # Cuts down on noise when the user's other Telegram clients
        # show a preview — we already write our own reply, no need to
        # blow up an URL into a card.
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": int(reply_to_message_id),
            # Don't error if the original message was deleted between
            # the inbound landing and our reply going out — degrade
            # gracefully to an unthreaded reply.
            "allow_sending_without_reply": True,
        }
    if reply_markup is not None:
        payload["reply_markup"] = dict(reply_markup)

    try:
        resp = httpx.post(
            _bot_endpoint(bot_token, "sendMessage"),
            json=payload,
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TelegramSendError(f"transport error: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("description") or resp.text[:300]
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise TelegramSendError(f"HTTP {resp.status_code}: {detail}")

    body = resp.json()
    if not body.get("ok"):
        raise TelegramSendError(
            f"sendMessage returned ok=false: {body.get('description')!r}"
        )
    message_id = int((body.get("result") or {}).get("message_id") or 0)
    logger.info(
        "Telegram sendMessage chat=%s message_id=%s body_len=%d",
        chat_id,
        message_id,
        len(text),
    )
    return message_id


# ---------------------------------------------------------------------------
# Attachments: getFile + download
# ---------------------------------------------------------------------------


@dataclass
class DownloadedFile:
    """File contents + metadata for a single Telegram attachment."""

    file_id: str
    mime_type: str
    file_bytes: bytes


def download_file(
    *,
    bot_token: str,
    file_id: str,
    hint_mime: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> DownloadedFile:
    """Resolve a Telegram ``file_id`` and pull the bytes off the file server.

    Bot API limits download to 20 MB per file — anything larger comes
    back from ``getFile`` with an empty ``file_path`` and we surface
    that as a :class:`TelegramReadError` so the caller can record an
    audit row instead of trying to recover.
    """
    if not bot_token:
        raise TelegramReadError("TELEGRAM_BOT_TOKEN missing.")

    try:
        meta_resp = httpx.post(
            _bot_endpoint(bot_token, "getFile"),
            json={"file_id": file_id},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TelegramReadError(f"getFile transport error: {exc}") from exc

    if meta_resp.status_code >= 400:
        raise TelegramReadError(
            f"getFile HTTP {meta_resp.status_code}: {meta_resp.text[:300]}"
        )

    meta = meta_resp.json()
    if not meta.get("ok"):
        raise TelegramReadError(
            f"getFile ok=false: {meta.get('description')!r}"
        )
    file_path = (meta.get("result") or {}).get("file_path")
    if not file_path:
        raise TelegramReadError(
            "getFile returned no file_path (likely >20 MB)."
        )

    try:
        file_resp = httpx.get(
            _file_endpoint(bot_token, file_path),
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TelegramReadError(f"file download transport error: {exc}") from exc

    if file_resp.status_code >= 400:
        raise TelegramReadError(
            f"file download HTTP {file_resp.status_code}"
        )

    mime = (
        file_resp.headers.get("content-type")
        or hint_mime
        or "application/octet-stream"
    ).split(";", 1)[0].strip()

    # Telegram serves OGG/Opus voice notes as application/octet-stream
    # sometimes; fall back to the path's extension when the header
    # isn't useful.
    if mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(file_path)
        if guessed:
            mime = guessed

    return DownloadedFile(
        file_id=file_id,
        mime_type=mime,
        file_bytes=file_resp.content,
    )


def extension_for_mime(mime: str) -> str:
    """Best-effort file extension for a MIME type ('image/jpeg' → '.jpg')."""
    ext = mimetypes.guess_extension(mime or "")
    if ext == ".jpe":
        return ".jpg"
    return ext or ".bin"


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------


@dataclass
class InboundAttachmentRef:
    """One attachment slot we extracted from an inbound message."""

    media_index: int
    kind: str  # 'photo' | 'document' | 'voice' | 'audio' | 'video' | …
    file_id: str
    mime_type: str  # best-effort, may be refined after download


@dataclass
class SharedContact:
    """The ``message.contact`` payload Telegram delivers when a user
    taps a ``request_contact`` keyboard button and confirms the share
    dialog. ``contact_user_id`` is the Telegram user id Telegram
    *says* the contact belongs to — we cross-check it against
    ``message.from.id`` to refuse impersonation attempts where one
    user shares somebody else's vCard."""

    phone_number: str
    contact_user_id: Optional[int]
    first_name: Optional[str]
    last_name: Optional[str]


@dataclass
class InboundTelegramMessage:
    """A parsed Telegram ``update`` we plan to act on (or audit)."""

    update_id: int
    chat_id: int
    message_id: int
    from_user_id: Optional[int]
    from_username: Optional[str]
    sender_display_name: Optional[str]
    is_bot_sender: bool
    body: Optional[str]
    attachments: List[InboundAttachmentRef] = field(default_factory=list)
    # Populated only for ``request_contact`` follow-ups; ``None`` for
    # every normal text/media message. Carries the phone number Avi
    # uses to auto-link an unknown sender to a Person row.
    shared_contact: Optional[SharedContact] = None
    # True iff the chat is 1:1 (private). We deliberately avoid
    # popping reply keyboards in groups — the prompt would interrupt
    # other members' UX, and "share your phone with the bot" only
    # makes sense as a private interaction.
    is_private_chat: bool = False

    @property
    def num_media(self) -> int:
        return len(self.attachments)

    @property
    def is_contact_share(self) -> bool:
        return self.shared_contact is not None


def is_actionable_message_update(update: Mapping[str, Any]) -> bool:
    """Return True iff ``update`` carries a normal user message we should
    consider replying to.

    We deliberately ignore edited messages, channel posts, and the
    various non-message update kinds (callback queries, etc.) — Avi
    only reacts to the same surface a human texting the bot would
    expect: a fresh inbound message in a private or group chat.
    """
    if not isinstance(update, Mapping):
        return False
    if "message" not in update:
        return False
    msg = update.get("message") or {}
    if not isinstance(msg, Mapping):
        return False
    return bool(msg.get("chat"))


def parse_inbound_update(update: Mapping[str, Any]) -> InboundTelegramMessage:
    """Pull the fields we care about out of one Bot API ``update`` JSON.

    Caller MUST have already filtered with :func:`is_actionable_message_update`.
    """
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}

    body = msg.get("text") or msg.get("caption")

    attachments: List[InboundAttachmentRef] = []

    # Photos arrive as a list of size variants — we only need the
    # largest (last entry) since we save the original file regardless.
    photos = msg.get("photo") or []
    if isinstance(photos, list) and photos:
        biggest = photos[-1]
        if isinstance(biggest, Mapping) and biggest.get("file_id"):
            attachments.append(
                InboundAttachmentRef(
                    media_index=len(attachments),
                    kind="photo",
                    file_id=str(biggest["file_id"]),
                    mime_type="image/jpeg",
                )
            )

    # Single-file slots — Telegram never sends more than one of these
    # per message, so we just probe each well-known key in order.
    for key, kind, default_mime in (
        ("document", "document", "application/octet-stream"),
        ("voice", "voice", "audio/ogg"),
        ("audio", "audio", "audio/mpeg"),
        ("video", "video", "video/mp4"),
        ("animation", "animation", "video/mp4"),
        ("sticker", "sticker", "image/webp"),
        ("video_note", "video", "video/mp4"),
    ):
        slot = msg.get(key)
        if isinstance(slot, Mapping) and slot.get("file_id"):
            attachments.append(
                InboundAttachmentRef(
                    media_index=len(attachments),
                    kind=kind,
                    file_id=str(slot["file_id"]),
                    mime_type=str(slot.get("mime_type") or default_mime),
                )
            )

    first = sender.get("first_name") or ""
    last = sender.get("last_name") or ""
    display_name = (first + " " + last).strip() or None

    # Contact share — only present after the user taps a keyboard
    # button with ``request_contact=true`` and confirms. The Bot API
    # contract is documented at
    # https://core.telegram.org/bots/api#contact.
    shared_contact: Optional[SharedContact] = None
    contact_blob = msg.get("contact")
    if isinstance(contact_blob, Mapping) and contact_blob.get("phone_number"):
        contact_uid = contact_blob.get("user_id")
        shared_contact = SharedContact(
            phone_number=str(contact_blob.get("phone_number")),
            contact_user_id=(
                int(contact_uid) if isinstance(contact_uid, int) else None
            ),
            first_name=contact_blob.get("first_name"),
            last_name=contact_blob.get("last_name"),
        )

    return InboundTelegramMessage(
        update_id=int(update.get("update_id") or 0),
        chat_id=int(chat.get("id") or 0),
        message_id=int(msg.get("message_id") or 0),
        from_user_id=(
            int(sender["id"]) if isinstance(sender.get("id"), int) else None
        ),
        from_username=sender.get("username"),
        sender_display_name=display_name,
        is_bot_sender=bool(sender.get("is_bot")),
        body=body,
        attachments=attachments,
        shared_contact=shared_contact,
        is_private_chat=str(chat.get("type") or "").lower() == "private",
    )


# ---------------------------------------------------------------------------
# Outbound formatting helper
# ---------------------------------------------------------------------------


def truncate_for_telegram(text: str, *, max_chars: int) -> str:
    """Trim ``text`` to fit a single Bot API ``sendMessage`` call.

    Telegram's hard limit on message text is 4096 chars; we expose a
    softer cap via settings so an over-eager LLM doesn't dump a wall
    of text on a phone screen. Cuts at the last whitespace before the
    limit so we don't slice a word in half.
    """
    text = (text or "").strip()
    cap = max(64, min(int(max_chars), 4096))
    if len(text) <= cap:
        return text
    cut = text[: cap - 1]
    last_space = cut.rfind(" ")
    if last_space > cap * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


__all__ = [
    "BotIdentity",
    "DownloadedFile",
    "InboundAttachmentRef",
    "InboundTelegramMessage",
    "SharedContact",
    "TelegramReadError",
    "TelegramSendError",
    "build_invite_url",
    "build_request_contact_keyboard",
    "download_file",
    "extension_for_mime",
    "get_me",
    "get_me_cached",
    "get_updates",
    "is_actionable_message_update",
    "parse_inbound_update",
    "remove_keyboard_markup",
    "send_message",
    "truncate_for_telegram",
]
