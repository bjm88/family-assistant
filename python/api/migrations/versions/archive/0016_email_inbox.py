"""Email inbox plumbing: ``live_sessions`` extensions + ``email_inbox_messages``.

Adds two pieces of infrastructure for the email-driven AI assistant:

1. Two new columns on ``live_sessions``:

   * ``source`` — ``'live'`` (camera/chat) or ``'email'`` (Gmail thread).
     Lets the history view tag rows with a "via email" badge without
     joining a sibling table.
   * ``external_thread_id`` — Gmail ``thread_id`` for email-sourced
     sessions. The email poller looks the session up by this column on
     each new message in the same thread, so a multi-turn email
     conversation reuses a single session row + transcript.

   A unique partial index (``family_id``, ``external_thread_id``) where
   ``external_thread_id IS NOT NULL`` keeps two pollers from racing on
   the same thread.

2. A brand-new ``email_inbox_messages`` table — the audit trail for
   every inbound email Avi laid eyes on. Includes the explicit security
   verdict (``status``) so it's trivial to answer "did Avi reply to
   this?" / "why was this stranger ignored?" without re-scanning Gmail.

Revision ID: 0016_email_inbox
Revises: 0015_agent_tasks
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_email_inbox"
down_revision: Union[str, None] = "0015_agent_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EMAIL_STATUSES = (
    "('processed_replied', 'ignored_unknown_sender', "
    "'ignored_self', 'ignored_bulk', 'ignored_already_seen', "
    "'failed')"
)


def upgrade() -> None:
    # ---- live_sessions extensions --------------------------------------
    op.add_column(
        "live_sessions",
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="live",
            comment=(
                "Which surface opened this session. 'live' = camera / "
                "in-page chat. 'email' = Gmail message routed through "
                "the email_inbox poller. UI uses this to badge rows "
                "differently in the history view."
            ),
        ),
    )
    op.add_column(
        "live_sessions",
        sa.Column(
            "external_thread_id",
            sa.Text(),
            nullable=True,
            comment=(
                "Opaque foreign id for the conversation upstream. For "
                "source='email' this is the Gmail thread_id, which lets "
                "the poller reuse one session per multi-turn email "
                "thread. NULL for source='live'."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email')",
    )
    op.create_index(
        "uq_live_sessions_external_thread",
        "live_sessions",
        ["family_id", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )

    # ---- email_inbox_messages -----------------------------------------
    op.create_table(
        "email_inbox_messages",
        sa.Column(
            "email_inbox_message_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.assistant_id", ondelete="CASCADE"),
            nullable=False,
            comment="Which assistant's mailbox the message landed in.",
        ),
        sa.Column(
            "gmail_message_id",
            sa.String(128),
            nullable=False,
            comment=(
                "Gmail's stable id for this message. Combined with "
                "assistant_id it is the dedup key the poller uses to "
                "guarantee at-most-once processing."
            ),
        ),
        sa.Column(
            "gmail_thread_id",
            sa.String(128),
            nullable=False,
            comment=(
                "Gmail thread id. Joined with live_sessions."
                "external_thread_id to retrieve the running transcript."
            ),
        ),
        sa.Column(
            "sender_email",
            sa.String(255),
            nullable=False,
            comment=(
                "Lowercased email address parsed from the From header. "
                "Compared (case-insensitive) against people.email_address "
                "to decide whether to reply."
            ),
        ),
        sa.Column(
            "sender_name",
            sa.String(255),
            nullable=True,
            comment="Display-name half of the From header, if present.",
        ),
        sa.Column(
            "subject",
            sa.Text(),
            nullable=True,
            comment="Subject header, kept verbatim for the audit trail.",
        ),
        sa.Column(
            "body_excerpt",
            sa.Text(),
            nullable=True,
            comment=(
                "First ~4 KB of the plain-text body. Stored so the "
                "history view can show what Avi actually saw without "
                "re-fetching from Gmail."
            ),
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Family member whose email_address matched the sender. "
                "NULL for ignored senders — kept that way on purpose so "
                "the audit row survives the person being deleted later."
            ),
        ),
        sa.Column(
            "status",
            sa.String(40),
            nullable=False,
            comment=(
                "Outcome verdict for this message. Drives the security "
                "audit: 'processed_replied' means we sent a reply, "
                "anything starting with 'ignored_' means we deliberately "
                "did not, 'failed' means we wanted to but the agent loop "
                "or Gmail send blew up."
            ),
        ),
        sa.Column(
            "status_reason",
            sa.Text(),
            nullable=True,
            comment="Human-readable detail for status (error, etc.).",
        ),
        sa.Column(
            "reply_message_id",
            sa.String(128),
            nullable=True,
            comment="Gmail id of the reply Avi sent, when status='processed_replied'.",
        ),
        sa.Column(
            "agent_task_id",
            sa.Integer(),
            sa.ForeignKey("agent_tasks.agent_task_id", ondelete="SET NULL"),
            nullable=True,
            comment="Audit link to the agent task that drafted the reply.",
        ),
        sa.Column(
            "live_session_id",
            sa.Integer(),
            sa.ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
            nullable=True,
            comment="Live session the inbound + reply messages were logged to.",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Timestamp Gmail records as the message arrival time.",
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the poller wrote this audit row (= when Avi reacted).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "assistant_id",
            "gmail_message_id",
            name="uq_email_inbox_message_per_assistant",
        ),
        sa.CheckConstraint(
            f"status IN {_EMAIL_STATUSES}",
            name="ck_email_inbox_messages_status",
        ),
        comment=(
            "One row per inbound email Avi inspected. Includes the "
            "security verdict so the family can audit exactly which "
            "messages got a reply, which were ignored, and why."
        ),
    )
    op.create_index(
        "ix_email_inbox_messages_assistant_processed",
        "email_inbox_messages",
        ["assistant_id", "processed_at"],
    )
    op.create_index(
        "ix_email_inbox_messages_thread",
        "email_inbox_messages",
        ["assistant_id", "gmail_thread_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_inbox_messages_thread",
        table_name="email_inbox_messages",
    )
    op.drop_index(
        "ix_email_inbox_messages_assistant_processed",
        table_name="email_inbox_messages",
    )
    op.drop_table("email_inbox_messages")
    op.drop_index(
        "uq_live_sessions_external_thread",
        table_name="live_sessions",
    )
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.drop_column("live_sessions", "external_thread_id")
    op.drop_column("live_sessions", "source")
