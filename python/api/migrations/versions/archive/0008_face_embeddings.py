"""Add ``face_embeddings`` table for the local AI assistant's recognizer.

Revision ID: 0008_face_embeddings
Revises: 0007_residences
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_face_embeddings"
down_revision: Union[str, None] = "0007_residences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "face_embeddings",
        sa.Column(
            "face_embedding_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_photo_id",
            sa.Integer(),
            sa.ForeignKey(
                "person_photos.person_photo_id", ondelete="CASCADE"
            ),
            nullable=False,
            unique=True,
            comment="Photo this embedding was extracted from.",
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="Person the embedding identifies.",
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Family tenant (denormalized for fast lookup).",
        ),
        sa.Column(
            "model_name",
            sa.String(length=60),
            nullable=False,
            server_default="buffalo_l",
            comment="InsightFace model pack used to produce the embedding.",
        ),
        sa.Column(
            "embedding_dim",
            sa.Integer(),
            nullable=False,
            server_default="512",
        ),
        sa.Column(
            "embedding_bytes",
            sa.LargeBinary(),
            nullable=False,
            comment="Raw float32 little-endian bytes (512 dims × 4 = 2048 bytes).",
        ),
        sa.Column(
            "bounding_box_json",
            sa.String(length=200),
            nullable=True,
            comment="JSON [x1, y1, x2, y2] of the detected face.",
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
            "One InsightFace embedding per person_photo flagged for face "
            "recognition. Loaded into memory at runtime and matched via "
            "cosine similarity against webcam frames."
        ),
    )
    op.create_index(
        "ix_face_embeddings_person_id", "face_embeddings", ["person_id"]
    )
    op.create_index(
        "ix_face_embeddings_family_id", "face_embeddings", ["family_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_face_embeddings_family_id", table_name="face_embeddings"
    )
    op.drop_index(
        "ix_face_embeddings_person_id", table_name="face_embeddings"
    )
    op.drop_table("face_embeddings")
