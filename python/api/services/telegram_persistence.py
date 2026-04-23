"""DB IO and message-formatting helpers for the Telegram inbox.

Carved out of :mod:`api.services.telegram_inbox` so the main inbox
file can focus on the loop / dispatch / agent invocation. Every
helper here is a pure function over a SQLAlchemy session + the
Telegram integration's typed payloads — no shared state, no
back-channels into the inbox module.

* Dedup / lookup queries against ``telegram_inbox_messages`` and the
  ``people`` table.
* Audit-row insert with the standard ``IntegrityError`` race
  fallback.
* Attachment downloader that turns a Bot-API ``InboundTelegramMessage``
  into ``TelegramInboxAttachment`` rows + on-disk blobs under
  ``resources/family/telegram_attachments``.
* Two formatting helpers (one for the human-readable transcript,
  one for the user-message envelope handed to the agent).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai import vision
from ..config import get_settings
from ..integrations import telegram


logger = logging.getLogger(__name__)


def already_processed(db: Session, telegram_update_id: int) -> bool:
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


def lookup_family_member_by_telegram(
    db: Session,
    *,
    user_id: Optional[int],
    username: Optional[str],
) -> Optional[models.Person]:
    """Find the unique person whose Telegram identity matches.

    Prefers ``telegram_user_id`` (stable, never re-assigned) and
    falls back to ``telegram_username`` (case-insensitive, mutable).
    If both are present we OR them in one query so a person who
    changed their @handle but kept the same id still resolves
    cleanly.
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


def save_audit(db: Session, **kwargs) -> models.TelegramInboxMessage:
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


def persist_attachments(
    db: Session,
    audit: models.TelegramInboxMessage,
    inbound: telegram.InboundTelegramMessage,
) -> tuple[List[dict], List[vision.RenderableAttachment], int]:
    """Download Telegram media, persist rows, and run the vision pipeline.

    Returns a 3-tuple of:

    1. ``meta`` — small ``{media_index, kind, mime_type, stored_path,
       file_size_bytes}`` dicts for the transcript ``meta`` blob.
    2. ``rendered`` — :class:`vision.RenderableAttachment` per
       attachment we analysed (in order), suitable for
       :func:`vision.render_attachments_for_prompt`.
    3. ``over_cap`` — count of attachments stored on disk but skipped
       past ``AI_ATTACHMENT_MAX_PER_MESSAGE``.

    Failures on one media file don't block the others — we log and
    keep going so a transient Bot API hiccup doesn't drop the whole
    reply.
    """
    if not inbound.attachments or audit.family_id is None:
        return [], [], 0
    settings = get_settings()
    bot_token = settings.TELEGRAM_BOT_TOKEN or ""
    meta: List[dict] = []
    inputs: List[vision.AttachmentInput] = []
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
        synthetic_name = (
            f"{ref.kind}-{ref.media_index}"
            f"{telegram.extension_for_mime(blob.mime_type) or ''}"
        )
        meta.append(
            {
                "media_index": ref.media_index,
                "kind": ref.kind,
                "mime_type": blob.mime_type,
                "stored_path": rel_path,
                "file_size_bytes": size,
                "filename": synthetic_name,
            }
        )
        inputs.append(
            vision.AttachmentInput(
                index=ref.media_index,
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


def format_inbound_for_log(
    inbound: telegram.InboundTelegramMessage,
    attachments: List[dict],
    *,
    attachment_block: str = "",
) -> str:
    """Render an inbound Telegram message so it reads naturally in history.

    ``attachment_block`` is the rendered, captioned list from the
    vision pipeline; falls back to a one-line ``[attachment N: …]``
    summary when no captions are available so the transcript is never
    silent about media.
    """
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
    if attachment_block:
        lines.append("")
        lines.append(attachment_block)
    elif attachments:
        lines.append("")
        for att in attachments:
            lines.append(
                f"[attachment {att['media_index']}: {att['kind']} "
                f"({att['mime_type']}, {att['file_size_bytes']} bytes)]"
            )
    return "\n".join(lines)


def format_user_message_for_agent(
    inbound: telegram.InboundTelegramMessage,
    person: models.Person,
    *,
    attachment_block: str = "",
) -> str:
    """Wrap the Telegram message so the agent knows the surface.

    The optional ``attachment_block`` is the captioned list from the
    vision pipeline, appended verbatim so the text-only Gemma model
    can reason about images and PDFs.
    """
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
    base = (
        f"[Telegram message from {name}{handle_hint}{media_hint}]\n\n"
        f"{(inbound.body or '').strip() or '(no text)'}"
    )
    if attachment_block:
        return f"{base}\n\n{attachment_block}"
    return base


__all__ = [
    "already_processed",
    "format_inbound_for_log",
    "format_user_message_for_agent",
    "lookup_family_member_by_telegram",
    "persist_attachments",
    "save_audit",
]
