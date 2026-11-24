"""One OPEN session per external thread instead of one session forever.

Text-message threads (SMS / WhatsApp / Telegram) now idle out like the
live chat does, and the next message starts a new session on the same
thread. The unique index therefore only covers sessions that are still
open.

Revision ID: 0010_open_thread_sessions
Revises: 0009_pending_attachments
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_open_thread_sessions"
down_revision: Union[str, None] = "0009_pending_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("uq_live_sessions_external_thread", table_name="live_sessions")
    op.create_index(
        "uq_live_sessions_open_external_thread",
        "live_sessions",
        ["family_id", "source", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL AND ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_live_sessions_open_external_thread", table_name="live_sessions")
    op.create_index(
        "uq_live_sessions_external_thread",
        "live_sessions",
        ["family_id", "source", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )
