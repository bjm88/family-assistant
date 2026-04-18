"""Add front/back image path columns to ``identity_documents``.

Revision ID: 0004_identity_document_images
Revises: 0003_assistants
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_identity_document_images"
down_revision: Union[str, None] = "0003_assistants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "identity_documents",
        sa.Column(
            "front_image_path",
            sa.String(length=500),
            nullable=True,
            comment=(
                "Path, relative to FA_STORAGE_ROOT, of a scan or photo of "
                "the front of the document (e.g. license face, passport "
                "photo page)."
            ),
        ),
    )
    op.add_column(
        "identity_documents",
        sa.Column(
            "back_image_path",
            sa.String(length=500),
            nullable=True,
            comment=(
                "Path, relative to FA_STORAGE_ROOT, of a scan or photo of "
                "the back of the document, when applicable."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("identity_documents", "back_image_path")
    op.drop_column("identity_documents", "front_image_path")
