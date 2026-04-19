"""The ``telegram_inbox_messages`` table — one row per inbound Telegram update.

Audit trail for the Telegram-driven AI assistant, structured
identically to ``sms_inbox_messages`` and ``email_inbox_messages`` so
the three surfaces stay easy to reason about side-by-side. Every time
the Bot API long-poll loop pulls a fresh ``update`` we write a row
here with the explicit security verdict (:attr:`status`):

* ``processed_replied``       — sender matched a registered family
  member's ``telegram_user_id`` (or ``telegram_username``), the agent
  loop ran, and Telegram accepted the reply send.
* ``ignored_unknown_sender``  — sender's Telegram identity did not
  match any ``Person.telegram_user_id`` / ``Person.telegram_username``.
  Avi never replies to strangers; this row is the receipt that proves
  it.
* ``ignored_self``            — the inbound came from our own bot
  account (loopback / accidental self-conversation).
* ``ignored_non_message``     — the update wasn't a normal text or
  media message (edits, channel posts, member-join events, …). We
  record the row so the audit trail is complete but never fire the
  agent loop.
* ``ignored_already_seen``    — dedup hit by ``telegram_update_id``;
  the long-poll offset bookkeeping should normally prevent this but
  we belt-and-brace it at the storage layer.
* ``failed``                  — wanted to reply but the agent loop or
  Bot API send blew up. ``status_reason`` carries the error text.

The unique constraint on ``telegram_update_id`` is what guarantees
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
# ``migrations/versions/0020_telegram_inbox.py`` (initial set) and
# ``migrations/versions/0022_telegram_inbox_status_prompt.py``
# (added ``prompted_for_contact_share``), and the Pydantic schema in
# ``schemas/telegram_inbox.py``.
TELEGRAM_INBOX_STATUSES: tuple[str, ...] = (
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_non_message",
    "ignored_already_seen",
    "failed",
    # We sent the unrecognised sender a one-tap "share your phone
    # number" keyboard so Avi can auto-bind them to a Person row
    # without an out-of-band invite. Set on the audit row for the
    # *original* unrecognised inbound that triggered the prompt;
    # the follow-up Contact reply lands as ``processed_replied``
    # with status_reason describing the binding.
    "prompted_for_contact_share",
)


# Coarse buckets we map Telegram's many media variants into. Kept as a
# plain string (not a Postgres ENUM) so a future ``video_note`` /
# ``contact`` / ``location`` doesn't cost a migration.
TELEGRAM_ATTACHMENT_KINDS: tuple[str, ...] = (
    "photo",
    "document",
    "voice",
    "audio",
    "video",
    "sticker",
    "animation",
)


class TelegramInboxMessage(Base, TimestampMixin):
    __tablename__ = "telegram_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "telegram_update_id",
            name="uq_telegram_inbox_update_id",
        ),
        CheckConstraint(
            "status IN ("
            "'processed_replied', 'ignored_unknown_sender', "
            "'ignored_self', 'ignored_non_message', "
            "'ignored_already_seen', 'failed', "
            "'prompted_for_contact_share'"
            ")",
            name="ck_telegram_inbox_messages_status",
        ),
        Index(
            "ix_telegram_inbox_messages_family_processed",
            "family_id",
            "processed_at",
        ),
        Index(
            "ix_telegram_inbox_messages_chat",
            "telegram_chat_id",
        ),
        {
            "comment": (
                "One row per inbound Telegram update Avi inspected, "
                "with an explicit security verdict so the family can "
                "audit exactly which messages got a reply, which were "
                "ignored, and why."
            )
        },
    )

    telegram_inbox_message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=True,
        comment=(
            "Family the inbound user resolved to. NULL for "
            "ignored_unknown_sender / failed rows that arrive before "
            "we know whose family it is."
        ),
    )
    telegram_update_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "Bot API update_id for this inbound message. Used as the "
            "dedup key so a re-delivered update never spawns a second "
            "agent run, and as the high-water mark for the long-poll "
            "offset."
        ),
    )
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "Telegram chat id the message arrived in (== from.id for "
            "private chats). Used as the reply destination and as the "
            "live_sessions.external_thread_id key."
        ),
    )
    telegram_message_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "Per-chat message id (message.message_id). Combined with "
            "telegram_chat_id this is what reply_to_message_id uses to "
            "thread our reply under the inbound."
        ),
    )
    telegram_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment=(
            "Numeric id of the sender (message.from.id). Compared "
            "against people.telegram_user_id to decide whether to "
            "reply. NULL only for channel posts that have no from."
        ),
    )
    telegram_username: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Sender's @username (without @), if they have one.",
    )
    sender_display_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment=(
            "Best-effort 'first_name last_name' assembly from the Bot "
            "API for the audit trail."
        ),
    )
    body: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Verbatim message text (or the caption when the message "
            "is a photo / video / document)."
        ),
    )
    num_media: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Count of attached files — every one gets its own row.",
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Family member whose Telegram identity matched. NULL for "
            "ignored senders — kept that way on purpose so the audit "
            "row survives the person being deleted later."
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
    reply_telegram_message_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Telegram message_id of the reply Avi sent.",
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
            "transcript for this Telegram thread."
        ),
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Wall-clock time the poller pulled this update.",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the poller finished writing this row.",
    )

    family: Mapped[Optional["Family"]] = relationship()  # noqa: F821
    person: Mapped[Optional["Person"]] = relationship()  # noqa: F821
    live_session: Mapped[Optional["LiveSession"]] = relationship()  # noqa: F821
    agent_task: Mapped[Optional["AgentTask"]] = relationship()  # noqa: F821
    attachments: Mapped[List["TelegramInboxAttachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="TelegramInboxAttachment.media_index",
    )


class TelegramInboxAttachment(Base, TimestampMixin):
    """One file attached to an inbound Telegram message."""

    __tablename__ = "telegram_inbox_attachments"
    __table_args__ = (
        UniqueConstraint(
            "telegram_inbox_message_id",
            "media_index",
            name="uq_telegram_inbox_attachment_slot",
        ),
        {
            "comment": (
                "Files attached to an inbound Telegram message, copied "
                "off the Bot API onto local storage so we have a "
                "permanent copy."
            )
        },
    )

    telegram_inbox_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    telegram_inbox_message_id: Mapped[int] = mapped_column(
        ForeignKey(
            "telegram_inbox_messages.telegram_inbox_message_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    media_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "Telegram attachment family — one of 'photo', 'document', "
            "'voice', 'audio', 'video', 'sticker', 'animation'."
        ),
    )
    telegram_file_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=(
            "Bot API file_id used with getFile to download the binary."
        ),
    )
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT.",
    )

    message: Mapped["TelegramInboxMessage"] = relationship(
        back_populates="attachments"
    )
