"""Widen email_inbox_attachments.gmail_attachment_id to TEXT.

Gmail's ``attachmentId`` is an opaque URL-safe base64 token with no
documented upper bound. In practice the values are well past the
``VARCHAR(255)`` we originally allocated for them — a single
attachment we tried to log in production produced a token long enough
to trip ``StringDataRightTruncation`` at insert time, which crashed
the email inbox handler AFTER the reply had already been sent (so the
audit row never landed). Switching the column to ``TEXT`` matches the
type already used for ``stored_path`` in the same table and removes
the size ceiling entirely.

Pure type widening: every existing value already fits, no data
rewrite is needed beyond the column-type alter, and the downgrade
path is a best-effort truncation (we cannot recover values longer
than 255 chars during a downgrade, but the column is purely
informational — the bytes themselves live on disk under
``stored_path`` and never need ``gmail_attachment_id`` to round-trip).

Revision ID: 0003_email_attachment_id_text
Revises: 0002_email_inbox_attachments
Create Date: 2026-04-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_email_attachment_id_text"
down_revision: Union[str, None] = "0002_email_inbox_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "email_inbox_attachments",
        "gmail_attachment_id",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
        existing_comment=(
            "Gmail's attachmentId for this part. Stored for "
            "forensics; the actual bytes are already on disk so we "
            "never have to re-fetch unless explicitly asked."
        ),
        comment=(
            "Gmail's attachmentId for this part. Stored for "
            "forensics; the actual bytes are already on disk so we "
            "never have to re-fetch unless explicitly asked. Typed "
            "as TEXT because Gmail's tokens are opaque URL-safe "
            "base64 with no documented upper bound — they regularly "
            "exceed 255 chars and have been observed past 1 KB."
        ),
    )


def downgrade() -> None:
    # USING clause truncates anything that wouldn't fit so the
    # downgrade itself doesn't fail; values longer than 255 chars
    # are unrecoverable but ``gmail_attachment_id`` is informational
    # only (the bytes are already on disk via ``stored_path``).
    op.execute(
        "ALTER TABLE email_inbox_attachments "
        "ALTER COLUMN gmail_attachment_id "
        "TYPE VARCHAR(255) "
        "USING substring(gmail_attachment_id FROM 1 FOR 255)"
    )
