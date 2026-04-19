"""The ``sms_inbox_messages`` table — one row per inbound Twilio SMS Avi sees.

This is the audit trail for the SMS-driven AI assistant, structured
identically to ``email_inbox_messages`` so the two surfaces stay easy
to reason about side-by-side. Every time the Twilio webhook fires we
write a row here with the explicit security verdict (:attr:`status`):

* ``processed_replied``       — sender matched a registered family
  member, the agent loop ran, and Twilio accepted the reply send.
* ``ignored_unknown_sender``  — sender's phone number did not match
  any ``Person.{mobile,home,work}_phone_number``. Avi never replies to
  strangers; this row is the receipt that proves it.
* ``ignored_self``            — the inbound came from our own Twilio
  number (loopback / status callback misroute).
* ``ignored_stop``            — the body was a STOP / UNSUBSCRIBE
  keyword. Twilio handles the carrier-level opt-out automatically;
  we record the row and move on.
* ``ignored_already_seen``    — dedup hit by ``twilio_message_sid``;
  Twilio retries on transient HTTP failures so we treat duplicate
  webhooks as a noop.
* ``failed``                  — wanted to reply but the agent loop or
  Twilio send blew up. ``status_reason`` carries the error text.

The unique constraint on ``twilio_message_sid`` is what guarantees
at-most-once processing across retries.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Keep in lock-step with the CHECK constraint in
# ``migrations/versions/0019_sms_inbox.py`` and the Pydantic schema
# in ``schemas/sms_inbox.py``.
SMS_INBOX_STATUSES: tuple[str, ...] = (
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_stop",
    "ignored_already_seen",
    "failed",
)


class SmsInboxMessage(Base, TimestampMixin):
    __tablename__ = "sms_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "twilio_message_sid",
            name="uq_sms_inbox_twilio_message_sid",
        ),
        CheckConstraint(
            "status IN ("
            "'processed_replied', 'ignored_unknown_sender', "
            "'ignored_self', 'ignored_stop', 'ignored_already_seen', "
            "'failed'"
            ")",
            name="ck_sms_inbox_messages_status",
        ),
        Index(
            "ix_sms_inbox_messages_family_processed",
            "family_id",
            "processed_at",
        ),
        Index(
            "ix_sms_inbox_messages_from_phone",
            "from_phone",
        ),
        {
            "comment": (
                "One row per inbound Twilio SMS/MMS Avi inspected, "
                "with an explicit security verdict so the family can "
                "audit exactly which messages got a reply, which were "
                "ignored, and why."
            )
        },
    )

    sms_inbox_message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=True,
        comment=(
            "Family the inbound number resolved to. NULL for "
            "ignored_unknown_sender / failed rows that arrive before "
            "we know whose family it is."
        ),
    )
    twilio_message_sid: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "Twilio's stable id (MessageSid) for the inbound message. "
            "Used as the dedup key so a re-delivered webhook never "
            "spawns a second agent run."
        ),
    )
    twilio_messaging_service_sid: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "MessagingServiceSid (MGxxx…) when the message arrived "
            "through a Messaging Service rather than a single number."
        ),
    )
    from_phone: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "Sender phone number in E.164 (e.g. +14155551234). "
            "Compared against people.{mobile,home,work}_phone_number."
        ),
    )
    to_phone: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="Twilio number that received the message (E.164).",
    )
    body: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Verbatim text body.",
    )
    num_media: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Twilio NumMedia field — count of MMS attachments.",
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Family member whose phone number matched from_phone. "
            "NULL for ignored senders — kept that way on purpose so "
            "the audit row survives the person being deleted later."
        ),
    )
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "Outcome verdict for this message. See module docstring "
            "for the full list."
        ),
    )
    status_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable detail for status (error message, etc.).",
    )
    reply_message_sid: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Twilio MessageSid of the reply Avi sent.",
    )
    agent_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_tasks.agent_task_id", ondelete="SET NULL"),
        nullable=True,
        comment="Audit link to the agent task that drafted the reply.",
    )
    live_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Live session row that holds the inbound + reply "
            "transcript for this SMS thread."
        ),
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Wall-clock time the webhook hit our server.",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the webhook handler finished writing this row.",
    )

    family: Mapped[Optional["Family"]] = relationship()  # noqa: F821
    person: Mapped[Optional["Person"]] = relationship()  # noqa: F821
    live_session: Mapped[Optional["LiveSession"]] = relationship()  # noqa: F821
    agent_task: Mapped[Optional["AgentTask"]] = relationship()  # noqa: F821
    attachments: Mapped[List["SmsInboxAttachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="SmsInboxAttachment.media_index",
    )


class SmsInboxAttachment(Base, TimestampMixin):
    """One MMS media file copied from Twilio onto local storage."""

    __tablename__ = "sms_inbox_attachments"
    __table_args__ = (
        UniqueConstraint(
            "sms_inbox_message_id",
            "media_index",
            name="uq_sms_inbox_attachment_slot",
        ),
        {
            "comment": (
                "MMS media files attached to an inbound SMS, copied "
                "off Twilio onto local storage so we have a permanent "
                "copy."
            )
        },
    )

    sms_inbox_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    sms_inbox_message_id: Mapped[int] = mapped_column(
        ForeignKey("sms_inbox_messages.sms_inbox_message_id", ondelete="CASCADE"),
        nullable=False,
    )
    media_index: Mapped[int] = mapped_column(Integer, nullable=False)
    twilio_media_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT.",
    )

    message: Mapped["SmsInboxMessage"] = relationship(back_populates="attachments")
