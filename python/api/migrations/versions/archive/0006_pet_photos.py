"""Add ``pet_photos`` table — additional photos of each pet.

Revision ID: 0006_pet_photos
Revises: 0005_goals_and_pets
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_pet_photos"
down_revision: Union[str, None] = "0005_goals_and_pets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pet_photos",
        sa.Column("pet_photo_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "pet_id",
            sa.Integer(),
            sa.ForeignKey("pets.pet_id", ondelete="CASCADE"),
            nullable=False,
            comment="Pet this photo belongs to.",
        ),
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=False,
            comment='Short human-readable name for the photo, e.g. "At the beach 2025".',
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Optional free-form description of where / when the photo was taken.",
        ),
        sa.Column(
            "stored_file_path",
            sa.String(length=500),
            nullable=False,
            comment="Path relative to FA_STORAGE_ROOT where the image is stored.",
        ),
        sa.Column("original_file_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
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
            "Photographs of a pet. Each row references the stored file on "
            "disk relative to FA_STORAGE_ROOT and carries an optional "
            "human-readable title and description."
        ),
    )
    op.create_index("ix_pet_photos_pet_id", "pet_photos", ["pet_id"])


def downgrade() -> None:
    op.drop_index("ix_pet_photos_pet_id", table_name="pet_photos")
    op.drop_table("pet_photos")
