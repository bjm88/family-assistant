"""Pydantic schemas for the SMS / WhatsApp inbox audit trail.

These are read-only — the only writer is the Twilio webhook handler in
``api.services.sms_inbox``. We expose them so a future admin page can
list "every SMS / WhatsApp message Avi has seen" without having to
re-derive the shape. The same table holds both surfaces, distinguished
by ``channel``.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from ._base import OrmModel


SmsInboxStatus = Literal[
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_stop",
    "ignored_already_seen",
    "failed",
]


SmsInboxChannel = Literal["sms", "whatsapp"]


class SmsInboxAttachmentRead(OrmModel):
    sms_inbox_attachment_id: int
    sms_inbox_message_id: int
    media_index: int
    twilio_media_url: str
    mime_type: str
    file_size_bytes: int
    stored_path: str
    created_at: datetime


class SmsInboxMessageRead(OrmModel):
    sms_inbox_message_id: int
    channel: SmsInboxChannel
    family_id: Optional[int]
    twilio_message_sid: str
    twilio_messaging_service_sid: Optional[str]
    from_phone: str
    to_phone: str
    body: Optional[str]
    num_media: int
    person_id: Optional[int]
    status: SmsInboxStatus
    status_reason: Optional[str]
    reply_message_sid: Optional[str]
    agent_task_id: Optional[int]
    live_session_id: Optional[int]
    received_at: datetime
    processed_at: datetime
    attachments: List[SmsInboxAttachmentRead] = []
