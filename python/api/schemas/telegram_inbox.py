"""Pydantic schemas for the Telegram inbox audit trail.

Read-only — the only writer is the Bot API long-poll loop in
``api.services.telegram_inbox``. Exposed so a future admin page can
list "every Telegram message Avi has seen" without re-deriving the
shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from ._base import OrmModel


TelegramInboxStatus = Literal[
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_non_message",
    "ignored_already_seen",
    "failed",
    "prompted_for_contact_share",
]


TelegramAttachmentKind = Literal[
    "photo",
    "document",
    "voice",
    "audio",
    "video",
    "sticker",
    "animation",
]


class TelegramInboxAttachmentRead(OrmModel):
    telegram_inbox_attachment_id: int
    telegram_inbox_message_id: int
    media_index: int
    kind: TelegramAttachmentKind
    telegram_file_id: str
    mime_type: str
    file_size_bytes: int
    stored_path: str
    created_at: datetime


class TelegramInboxMessageRead(OrmModel):
    telegram_inbox_message_id: int
    family_id: Optional[int]
    telegram_update_id: int
    telegram_chat_id: int
    telegram_message_id: int
    telegram_user_id: Optional[int]
    telegram_username: Optional[str]
    sender_display_name: Optional[str]
    body: Optional[str]
    num_media: int
    person_id: Optional[int]
    status: TelegramInboxStatus
    status_reason: Optional[str]
    reply_telegram_message_id: Optional[int]
    agent_task_id: Optional[int]
    live_session_id: Optional[int]
    received_at: datetime
    processed_at: datetime
    attachments: List[TelegramInboxAttachmentRead] = []
