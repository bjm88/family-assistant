"""Add ``pending_attachments`` — files Avi asked the sender about.

When Avi can't tell what an attachment-only message is, or whose it
is, he asks instead of guessing. Inbound surfaces don't carry chat
history, so the open question and the file are remembered here and
handed to the agent with the sender's next message.

Revision ID: 0009_pending_attachments
Revises: 0008_document_extracted_text
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_pending_attachments"
down_revision: Union[str, None] = "0008_document_extracted_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_attachments",
        sa.Column("pending_attachment_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The sender Avi is waiting to hear back from.",
        ),
        sa.Column("channel", sa.String(length=20), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "stored_path",
            sa.String(length=500),
            nullable=False,
            comment="Relative path under FA_STORAGE_ROOT of the inbound file.",
        ),
        sa.Column(
            "question",
            sa.Text(),
            nullable=False,
            comment="What Avi found and what he asked, as shown to the agent.",
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the follow-up was handed to the agent; NULL = still waiting.",
        ),
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
        comment=(
            "Inbound attachments Avi could not file on his own (unknown "
            "holder, unclear subject, unreadable). Avi asked the sender; "
            "their next message is handed to the agent together with the "
            "file so the answer can be acted on."
        ),
    )
    op.create_index(
        "ix_pending_attachments_person_id", "pending_attachments", ["person_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_pending_attachments_person_id", table_name="pending_attachments")
    op.drop_table("pending_attachments")
