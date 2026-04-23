"""Add email_inbox_attachments table.

Adds a new table to store the binary payloads of attachments that
arrive on emails Avi answers. Existing rows in
``email_inbox_messages`` are unaffected — this is a pure additive
migration so it cannot regress any data.

Mirrors the ``sms_inbox_attachments`` and
``telegram_inbox_attachments`` schemas so the multi-channel attachment
pipeline (vision adapter + RAG insertion) treats all three the same.

Revision ID: 0002_email_inbox_attachments
Revises: 0001_initial_schema
Create Date: 2026-04-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002_email_inbox_attachments"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_inbox_attachments",
        sa.Column(
            "email_inbox_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "email_inbox_message_id",
            sa.Integer(),
            sa.ForeignKey(
                "email_inbox_messages.email_inbox_message_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "media_index",
            sa.Integer(),
            nullable=False,
            comment=(
                "1-based index within the parent email. Matches the "
                "order the parts were walked in, so 'Attachment 1' in "
                "the agent prompt always refers to media_index=1 here."
            ),
        ),
        sa.Column(
            "gmail_attachment_id",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Gmail's attachmentId for this part. Stored for "
                "forensics; the actual bytes are already on disk so we "
                "never have to re-fetch unless explicitly asked."
            ),
        ),
        sa.Column(
            "filename",
            sa.String(length=512),
            nullable=True,
            comment=(
                "Original filename from the Content-Disposition header. "
                "May be NULL for inline images that came in without a "
                "name."
            ),
        ),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "stored_path",
            sa.Text(),
            nullable=False,
            comment="Path relative to FA_STORAGE_ROOT.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "email_inbox_message_id",
            "media_index",
            name="uq_email_inbox_attachment_slot",
        ),
        comment=(
            "Files attached to an inbound email, downloaded from Gmail "
            "and stored locally so Avi has a permanent copy and can run "
            "the multimodal vision/text-extraction pipeline on them."
        ),
    )


def downgrade() -> None:
    op.drop_table("email_inbox_attachments")
