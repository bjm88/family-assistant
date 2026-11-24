"""Add ``documents.extracted_text`` for OCR'd / extracted document text.

Attachments Avi files on his own (a bill or a school form sent with no
caption) are read with a vision model first; keeping the transcription
on the row lets Avi answer later questions ("what was the total on the
October ConEd bill?") without re-reading the file.

Revision ID: 0008_document_extracted_text
Revises: 0007_person_websites
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_document_extracted_text"
down_revision: Union[str, None] = "0007_person_websites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "extracted_text",
            sa.Text(),
            nullable=True,
            comment=(
                "Text read from the file (PDF text layer or vision OCR). "
                "Government ID, SSN and full account numbers are reduced "
                "to their last four characters before storage."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "extracted_text")
