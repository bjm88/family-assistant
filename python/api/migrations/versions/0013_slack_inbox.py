"""Slack as an inbound channel: audit tables + ``live_sessions.source``.

Adds ``slack_inbox_messages`` / ``slack_inbox_attachments`` (the same
audit trail SMS, WhatsApp and Telegram have) and lets a live session
be opened by ``source='slack'``.

Revision ID: 0013_slack_inbox
Revises: 0012_contact_connections
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# alembic_version.version_num is VARCHAR(32) — keep revision ids short.
revision: str = "0013_slack_inbox"
down_revision: Union[str, None] = "0012_contact_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_SOURCES = ("live", "email", "sms", "telegram", "whatsapp")
_NEW_SOURCES = _OLD_SOURCES + ("slack",)


def _source_sql(values: tuple[str, ...]) -> str:
    return "source IN (" + ", ".join(repr(v) for v in values) + ")"


def _replace_source_check(values: tuple[str, ...]) -> None:
    op.execute(
        "ALTER TABLE live_sessions DROP CONSTRAINT IF EXISTS ck_live_sessions_source"
    )
    op.create_check_constraint(
        "ck_live_sessions_source", "live_sessions", _source_sql(values)
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp when the row was first inserted.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp of the most recent update to the row.",
        ),
    ]


def upgrade() -> None:
    _replace_source_check(_NEW_SOURCES)

    # 0001 builds a brand-new database from the current models, so on a
    # fresh install these tables already exist by the time we get here.
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("slack_inbox_messages"):
        op.create_table(
            "slack_inbox_messages",
            sa.Column(
                "slack_inbox_message_id",
                sa.Integer(),
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column(
                "family_id",
                sa.Integer(),
                sa.ForeignKey("families.family_id", ondelete="CASCADE"),
                nullable=True,
                comment="Family the sender resolved to. NULL for unknown senders.",
            ),
            sa.Column(
                "slack_team_id",
                sa.String(length=32),
                nullable=False,
                comment="Slack workspace id (T…).",
            ),
            sa.Column(
                "slack_channel_id",
                sa.String(length=32),
                nullable=False,
                comment=(
                    "Conversation the message arrived in (D… for a direct "
                    "message). The reply goes back here, and it is the "
                    "live_sessions.external_thread_id key."
                ),
            ),
            sa.Column(
                "slack_message_ts",
                sa.String(length=32),
                nullable=False,
                comment="Slack's per-channel message id, e.g. '1732468800.123456'.",
            ),
            sa.Column(
                "slack_thread_ts",
                sa.String(length=32),
                nullable=True,
                comment="Parent message ts when the message was sent inside a thread.",
            ),
            sa.Column(
                "slack_event_id",
                sa.String(length=64),
                nullable=True,
                comment=(
                    "Events API event_id. NULL for messages found by the "
                    "catch-up pass."
                ),
            ),
            sa.Column(
                "channel_type",
                sa.String(length=16),
                nullable=False,
                comment="'im' (direct message), 'mpim', 'channel' or 'group'.",
            ),
            sa.Column(
                "via",
                sa.String(length=8),
                nullable=False,
                comment=(
                    "Which of Avi's Slack identities received it: 'bot' (the "
                    "app) or 'user' (Avi's member account). The reply is sent "
                    "as the same identity."
                ),
            ),
            sa.Column(
                "slack_user_id",
                sa.String(length=32),
                nullable=True,
                comment="Sender's Slack user id (U…).",
            ),
            sa.Column(
                "sender_email",
                sa.String(length=255),
                nullable=True,
                comment=(
                    "Email on the sender's Slack account when the message "
                    "arrived. Matched against people.email_address (and their "
                    "jobs' work_email) to decide whether to reply."
                ),
            ),
            sa.Column("sender_display_name", sa.String(length=255), nullable=True),
            sa.Column(
                "body",
                sa.Text(),
                nullable=True,
                comment="Message text with Slack markup resolved.",
            ),
            sa.Column(
                "num_media",
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment="Count of files shared with the message.",
            ),
            sa.Column(
                "person_id",
                sa.Integer(),
                sa.ForeignKey("people.person_id", ondelete="SET NULL"),
                nullable=True,
                comment="Family member the sender matched. NULL for ignored senders.",
            ),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                comment="Outcome verdict. See module docstring.",
            ),
            sa.Column(
                "status_reason",
                sa.Text(),
                nullable=True,
                comment="Human-readable detail for status (error message, etc.).",
            ),
            sa.Column(
                "reply_message_ts",
                sa.String(length=32),
                nullable=True,
                comment="ts of the reply Avi posted.",
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
                comment="Live session holding the transcript for this conversation.",
            ),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
                comment="Wall-clock time Avi received the message.",
            ),
            sa.Column(
                "processed_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
                comment="When this row was written.",
            ),
            *_timestamps(),
            sa.UniqueConstraint(
                "slack_team_id",
                "slack_channel_id",
                "slack_message_ts",
                name="uq_slack_inbox_message",
            ),
            sa.CheckConstraint(
                "status IN ('processed_replied', 'ignored_unknown_sender', 'failed')",
                name="ck_slack_inbox_messages_status",
            ),
            sa.CheckConstraint(
                "via IN ('bot', 'user')",
                name="ck_slack_inbox_messages_via",
            ),
            comment=(
                "One row per Slack message a person sent Avi, with the "
                "verdict: replied, ignored (unknown sender) or failed."
            ),
        )
        op.create_index(
            "ix_slack_inbox_messages_family_processed",
            "slack_inbox_messages",
            ["family_id", "processed_at"],
        )

    if not inspector.has_table("slack_inbox_attachments"):
        op.create_table(
            "slack_inbox_attachments",
            sa.Column(
                "slack_inbox_attachment_id",
                sa.Integer(),
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column(
                "slack_inbox_message_id",
                sa.Integer(),
                sa.ForeignKey(
                    "slack_inbox_messages.slack_inbox_message_id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "media_index",
                sa.Integer(),
                nullable=False,
                comment="1-based position in the message, as shown to the agent.",
            ),
            sa.Column(
                "slack_file_id",
                sa.String(length=32),
                nullable=False,
                comment="Slack file id (F…).",
            ),
            sa.Column("filename", sa.String(length=255), nullable=True),
            sa.Column("mime_type", sa.String(length=120), nullable=False),
            sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
            sa.Column(
                "stored_path",
                sa.Text(),
                nullable=False,
                comment="Path relative to FA_STORAGE_ROOT.",
            ),
            *_timestamps(),
            sa.UniqueConstraint(
                "slack_inbox_message_id",
                "media_index",
                name="uq_slack_inbox_attachment_slot",
            ),
            comment=(
                "Files shared with an inbound Slack message, copied "
                "onto local storage so we keep a permanent copy."
            ),
        )


def downgrade() -> None:
    op.drop_table("slack_inbox_attachments")
    op.drop_index(
        "ix_slack_inbox_messages_family_processed",
        table_name="slack_inbox_messages",
    )
    op.drop_table("slack_inbox_messages")
    # Fails while any live_sessions row still has source='slack' — remove
    # or re-label those first; this migration will not delete transcripts.
    _replace_source_check(_OLD_SOURCES)
