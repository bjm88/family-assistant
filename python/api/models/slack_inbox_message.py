"""The ``slack_inbox_messages`` table — one row per Slack message Avi weighed.

Audit trail for the Slack channel, shaped like ``sms_inbox_messages``
and ``telegram_inbox_messages`` so the surfaces read side by side.
Every message a *person* sends Avi on Slack gets a row with the
verdict (:attr:`status`):

* ``processed_replied``      — the sender's Slack account email matched
  a family member, the agent ran and Slack accepted the reply.
* ``ignored_unknown_sender`` — nobody in any household has that email.
  Avi stays silent; this row is the receipt.
* ``failed``                 — Avi meant to reply but the agent or the
  send failed (or the channel is switched off). ``status_reason`` says
  which.

Events that are not a person talking to Avi — his own messages echoing
back, edits, deletions, channel chatter that doesn't mention him — are
dropped before this table and only logged.

``(slack_team_id, slack_channel_id, slack_message_ts)`` is unique: a
Slack message is identified by its channel and timestamp, so a webhook
retry or the downtime catch-up can never run the agent twice for it.
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


# Keep in step with the CHECK constraint in
# ``migrations/versions/0013_slack_inbox.py``.
SLACK_INBOX_STATUSES: tuple[str, ...] = (
    "processed_replied",
    "ignored_unknown_sender",
    "failed",
)

# Which of Avi's Slack identities received the message.
SLACK_INBOX_VIA: tuple[str, ...] = ("bot", "user")


class SlackInboxMessage(Base, TimestampMixin):
    __tablename__ = "slack_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "slack_team_id",
            "slack_channel_id",
            "slack_message_ts",
            name="uq_slack_inbox_message",
        ),
        CheckConstraint(
            "status IN ('processed_replied', 'ignored_unknown_sender', 'failed')",
            name="ck_slack_inbox_messages_status",
        ),
        CheckConstraint(
            "via IN ('bot', 'user')",
            name="ck_slack_inbox_messages_via",
        ),
        Index(
            "ix_slack_inbox_messages_family_processed",
            "family_id",
            "processed_at",
        ),
        {
            "comment": (
                "One row per Slack message a person sent Avi, with the "
                "verdict: replied, ignored (unknown sender) or failed."
            )
        },
    )

    slack_inbox_message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=True,
        comment="Family the sender resolved to. NULL for unknown senders.",
    )
    slack_team_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Slack workspace id (T…)."
    )
    slack_channel_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "Conversation the message arrived in (D… for a direct "
            "message). The reply goes back here, and it is the "
            "live_sessions.external_thread_id key."
        ),
    )
    slack_message_ts: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Slack's per-channel message id, e.g. '1732468800.123456'.",
    )
    slack_thread_ts: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment="Parent message ts when the message was sent inside a thread.",
    )
    slack_event_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Events API event_id. NULL for messages found by the catch-up pass.",
    )
    channel_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="'im' (direct message), 'mpim', 'channel' or 'group'.",
    )
    via: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment=(
            "Which of Avi's Slack identities received it: 'bot' (the "
            "app) or 'user' (Avi's member account). The reply is sent "
            "as the same identity."
        ),
    )
    slack_user_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="Sender's Slack user id (U…)."
    )
    sender_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment=(
            "Email on the sender's Slack account when the message "
            "arrived. Matched against people.email_address (and their "
            "jobs' work_email) to decide whether to reply."
        ),
    )
    sender_display_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    body: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="Message text with Slack markup resolved."
    )
    num_media: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Count of files shared with the message.",
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment="Family member the sender matched. NULL for ignored senders.",
    )
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="Outcome verdict. See module docstring.",
    )
    status_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable detail for status (error message, etc.).",
    )
    reply_message_ts: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="ts of the reply Avi posted."
    )
    agent_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_tasks.agent_task_id", ondelete="SET NULL"),
        nullable=True,
        comment="Audit link to the agent task that drafted the reply.",
    )
    live_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
        nullable=True,
        comment="Live session holding the transcript for this conversation.",
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Wall-clock time Avi received the message.",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this row was written.",
    )

    family: Mapped[Optional["Family"]] = relationship()  # noqa: F821
    person: Mapped[Optional["Person"]] = relationship()  # noqa: F821
    live_session: Mapped[Optional["LiveSession"]] = relationship()  # noqa: F821
    agent_task: Mapped[Optional["AgentTask"]] = relationship()  # noqa: F821
    attachments: Mapped[List["SlackInboxAttachment"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="SlackInboxAttachment.media_index",
    )


class SlackInboxAttachment(Base, TimestampMixin):
    """One file shared with an inbound Slack message."""

    __tablename__ = "slack_inbox_attachments"
    __table_args__ = (
        UniqueConstraint(
            "slack_inbox_message_id",
            "media_index",
            name="uq_slack_inbox_attachment_slot",
        ),
        {
            "comment": (
                "Files shared with an inbound Slack message, copied "
                "onto local storage so we keep a permanent copy."
            )
        },
    )

    slack_inbox_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    slack_inbox_message_id: Mapped[int] = mapped_column(
        ForeignKey(
            "slack_inbox_messages.slack_inbox_message_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    media_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-based position in the message, as shown to the agent.",
    )
    slack_file_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Slack file id (F…)."
    )
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT.",
    )

    message: Mapped["SlackInboxMessage"] = relationship(
        back_populates="attachments"
    )
