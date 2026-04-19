"""The ``email_inbox_messages`` table — one row per inbound email Avi sees.

This is the audit trail for the email-driven AI assistant. Every time
the inbox poller fetches a Gmail message it writes a row here with the
explicit security verdict (:attr:`status`):

* ``processed_replied``       — sender matched a registered family
  member, the agent loop ran, and Gmail accepted the reply send.
* ``ignored_unknown_sender``  — sender's email did not match any
  ``Person.email_address`` in the assistant's family. Avi never replies
  to strangers; this row is the receipt that proves it.
* ``ignored_self``            — the message was Avi's own outbound copy
  arriving back via the All Mail label.
* ``ignored_bulk``            — looked like a mailing list / auto-reply
  / list-unsubscribe traffic.
* ``ignored_already_seen``    — dedup hit, message id was already in
  the table (defence in depth — the poller also filters at the SELECT
  layer).
* ``failed``                  — wanted to reply but the agent loop or
  Gmail send blew up. ``status_reason`` carries the error text.

The unique constraint on ``(assistant_id, gmail_message_id)`` is what
guarantees at-most-once processing even when two pollers run in
parallel (or one is restarted mid-tick).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Keep in lock-step with the CHECK constraint in
# ``migrations/versions/0016_email_inbox.py`` and the Pydantic schema
# in ``schemas/email_inbox.py``.
EMAIL_INBOX_STATUSES: tuple[str, ...] = (
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_bulk",
    "ignored_already_seen",
    "failed",
)


class EmailInboxMessage(Base, TimestampMixin):
    __tablename__ = "email_inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "assistant_id",
            "gmail_message_id",
            name="uq_email_inbox_message_per_assistant",
        ),
        CheckConstraint(
            "status IN ("
            "'processed_replied', 'ignored_unknown_sender', "
            "'ignored_self', 'ignored_bulk', 'ignored_already_seen', "
            "'failed'"
            ")",
            name="ck_email_inbox_messages_status",
        ),
        Index(
            "ix_email_inbox_messages_assistant_processed",
            "assistant_id",
            "processed_at",
        ),
        Index(
            "ix_email_inbox_messages_thread",
            "assistant_id",
            "gmail_thread_id",
        ),
        {
            "comment": (
                "One row per inbound email Avi inspected, with an "
                "explicit security verdict so the family can audit "
                "exactly which messages got a reply, which were "
                "ignored, and why."
            )
        },
    )

    email_inbox_message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    assistant_id: Mapped[int] = mapped_column(
        ForeignKey("assistants.assistant_id", ondelete="CASCADE"),
        nullable=False,
        comment="Which assistant's mailbox the message landed in.",
    )
    gmail_message_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "Gmail's stable id for this message. Combined with "
            "``assistant_id`` it is the dedup key the poller uses to "
            "guarantee at-most-once processing."
        ),
    )
    gmail_thread_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "Gmail thread id. Joined with "
            "``live_sessions.external_thread_id`` to retrieve the "
            "running transcript for the conversation."
        ),
    )
    sender_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=(
            "Lowercased email address parsed from the From header. "
            "Compared (case-insensitive) against "
            "``people.email_address`` to decide whether to reply."
        ),
    )
    sender_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Display-name half of the From header, if present.",
    )
    subject: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Subject header, kept verbatim for the audit trail.",
    )
    body_excerpt: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "First ~4 KB of the plain-text body. Stored so the history "
            "view can show what Avi actually saw without re-fetching "
            "from Gmail."
        ),
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Family member whose email_address matched the sender. "
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
    reply_message_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="Gmail id of the reply Avi sent, when status='processed_replied'.",
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
            "transcript for this email."
        ),
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Timestamp Gmail records as the message arrival time.",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the poller wrote this audit row (= when Avi reacted).",
    )

    assistant: Mapped["Assistant"] = relationship()  # noqa: F821
    person: Mapped[Optional["Person"]] = relationship()  # noqa: F821
    live_session: Mapped[Optional["LiveSession"]] = relationship()  # noqa: F821
    agent_task: Mapped[Optional["AgentTask"]] = relationship()  # noqa: F821
