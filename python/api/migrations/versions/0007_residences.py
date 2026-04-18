"""Add ``residences`` and ``residence_photos`` tables.

Revision ID: 0007_residences
Revises: 0006_pet_photos
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_residences"
down_revision: Union[str, None] = "0006_pet_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "residences",
        sa.Column(
            "residence_id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Household this residence belongs to.",
        ),
        sa.Column(
            "label",
            sa.String(length=80),
            nullable=False,
            comment=(
                'Short nickname, e.g. "Main house", "Lake cabin", "NYC '
                'apartment".'
            ),
        ),
        sa.Column("street_line_1", sa.String(length=200), nullable=False),
        sa.Column("street_line_2", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("state_or_region", sa.String(length=80), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column(
            "country",
            sa.String(length=80),
            nullable=False,
            server_default="United States",
        ),
        sa.Column(
            "is_primary_residence",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "True for the family's primary residence. The residences "
                "router guarantees at most one row per family has this set."
            ),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "Physical homes belonging to a family (main house, cabin, "
            "pied-à-terre, rental). Distinct from the legacy addresses "
            "table because residences carry photos and a primary-home "
            "flag. Exactly one residence per family should have "
            "is_primary_residence=true; the residences router enforces "
            "this automatically on create/update."
        ),
    )
    op.create_index("ix_residences_family_id", "residences", ["family_id"])

    op.create_table(
        "residence_photos",
        sa.Column(
            "residence_photo_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "residence_id",
            sa.Integer(),
            sa.ForeignKey("residences.residence_id", ondelete="CASCADE"),
            nullable=False,
            comment="Residence this photo belongs to.",
        ),
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=False,
            comment='Short human-readable name for the photo, e.g. "Front yard".',
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
            "Photographs of a residence. Each row references the stored "
            "file on disk relative to FA_STORAGE_ROOT and carries an "
            "optional human-readable title and description."
        ),
    )
    op.create_index(
        "ix_residence_photos_residence_id",
        "residence_photos",
        ["residence_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_residence_photos_residence_id", table_name="residence_photos"
    )
    op.drop_table("residence_photos")
    op.drop_index("ix_residences_family_id", table_name="residences")
    op.drop_table("residences")
