"""Add ``live_sessions``, ``live_session_participants``, ``live_session_messages``.

These tables bracket every live AI-assistant interaction with a
household. A session is opened automatically when Avi first sees a face
or receives a chat message, closed after 30 minutes of no activity, and
tracks who Avi greeted so "Hi <name>" doesn't repeat within the same
window. Messages are logged server-side so the history view can replay
a full conversation without the client being online.

Revision ID: 0009_live_sessions
Revises: 0008_face_embeddings
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0009_live_sessions"
down_revision: Union[str, None] = "0008_face_embeddings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- live_sessions ----------------------------------------------------
    op.create_table(
        "live_sessions",
        sa.Column(
            "live_session_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Family that owns this live session.",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When Avi opened the session (first face or first chat).",
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the session closed. NULL while the session is still "
                "considered active by the idle-timeout sweeper."
            ),
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment=(
                "Updated on every new message or participant. Drives the "
                "30-minute inactivity auto-close."
            ),
        ),
        sa.Column(
            "start_context",
            sa.Text(),
            nullable=True,
            comment=(
                "Short tag describing why the session was opened, e.g. "
                "'page_opened', 'face_recognized:5', 'chat_initiated'."
            ),
        ),
        sa.Column(
            "end_reason",
            sa.String(length=32),
            nullable=True,
            comment=(
                "One of 'timeout' (idle sweep), 'manual' (closed from UI), "
                "'superseded' (a newer session took over), or NULL while "
                "still active."
            ),
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
        comment=(
            "One row per continuous AI-assistant interaction with a family. "
            "Messages and participants hang off this row so the history "
            "view can replay the whole conversation."
        ),
    )
    op.create_index(
        "ix_live_sessions_family_id", "live_sessions", ["family_id"]
    )
    # Partial index makes "find the single active session per family"
    # blazingly fast — the common path during a live interaction.
    op.create_index(
        "ix_live_sessions_active_by_family",
        "live_sessions",
        ["family_id", "last_activity_at"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    # ---- live_session_participants ----------------------------------------
    op.create_table(
        "live_session_participants",
        sa.Column(
            "live_session_participant_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "live_session_id",
            sa.Integer(),
            sa.ForeignKey(
                "live_sessions.live_session_id", ondelete="CASCADE"
            ),
            nullable=False,
            comment="Session this participant is part of.",
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The recognized family member.",
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="First moment the camera matched this person in the session.",
        ),
        sa.Column(
            "greeted_already",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment=(
                "False until Avi has said 'Hi <name>' to this person in "
                "this session. Flipped atomically by /greet to prevent "
                "repeated greetings as the face drifts in and out of view."
            ),
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
            "live_session_id",
            "person_id",
            name="uq_live_session_participant",
        ),
        comment=(
            "Join table: one row per (session, person). ``greeted_already`` "
            "gates repeat greetings within the same window."
        ),
    )
    op.create_index(
        "ix_live_session_participants_session",
        "live_session_participants",
        ["live_session_id"],
    )

    # ---- live_session_messages --------------------------------------------
    op.create_table(
        "live_session_messages",
        sa.Column(
            "live_session_message_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "live_session_id",
            sa.Integer(),
            sa.ForeignKey(
                "live_sessions.live_session_id", ondelete="CASCADE"
            ),
            nullable=False,
            comment="Session this message belongs to.",
        ),
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            comment=(
                "Speaker role: 'user' (a family member), 'assistant' (Avi), "
                "or 'system' (automated note, e.g. 'session started')."
            ),
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "For role='user', the person we attribute the message to "
                "(derived from the active face-recognition result). NULL "
                "for assistant/system messages."
            ),
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Plain-text message body (what was said or typed).",
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Free-form structured context: model name, goal reference, "
                "RAG preview, latency, future attachment descriptors."
            ),
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
        comment=(
            "Transcript of a live session. One row per utterance or "
            "system note, ordered by created_at."
        ),
    )
    op.create_index(
        "ix_live_session_messages_session",
        "live_session_messages",
        ["live_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_session_messages_session",
        table_name="live_session_messages",
    )
    op.drop_table("live_session_messages")
    op.drop_index(
        "ix_live_session_participants_session",
        table_name="live_session_participants",
    )
    op.drop_table("live_session_participants")
    op.drop_index(
        "ix_live_sessions_active_by_family",
        table_name="live_sessions",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index(
        "ix_live_sessions_family_id", table_name="live_sessions"
    )
    op.drop_table("live_sessions")
