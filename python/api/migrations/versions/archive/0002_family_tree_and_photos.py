"""Rename primary relationship column; add person_photos and person_relationships.

* Renames ``people.relationship_to_head_of_household`` →
  ``people.primary_family_relationship`` and updates its comment.
* Creates ``person_photos`` — additional photos per person with a
  ``use_for_face_recognition`` flag for the future Avi enrollment step.
* Creates ``person_relationships`` — the atomic family-tree edges
  (``parent_of``, ``spouse_of``); siblings and other relationships are
  derived at query time.

Revision ID: 0002_family_tree_and_photos
Revises: 0001_initial_schema
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002_family_tree_and_photos"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_PRIMARY_RELATIONSHIP_COMMENT = (
    "Primary relationship label from the perspective of the family "
    "manager (self, spouse, parent, child, sibling, guardian, "
    "grandparent, grandchild, etc.). This is a convenience label "
    "only; the authoritative family tree lives in "
    "person_relationships."
)


def upgrade() -> None:
    op.alter_column(
        "people",
        "relationship_to_head_of_household",
        new_column_name="primary_family_relationship",
        existing_type=sa.String(length=40),
        existing_nullable=True,
        comment=NEW_PRIMARY_RELATIONSHIP_COMMENT,
        existing_comment=(
            "Relationship label from the perspective of the family manager: "
            "self, spouse, child, parent, sibling, guardian, etc."
        ),
    )

    op.create_table(
        "person_photos",
        sa.Column("person_photo_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "title",
            sa.String(length=200),
            nullable=False,
            comment='Short human-readable name for the photo, e.g. "Family reunion 2024".',
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Optional free-form description of where / when the photo was taken.",
        ),
        sa.Column(
            "use_for_face_recognition",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment=(
                "If true, the photo is included in the face-recognition "
                "enrollment set for this person."
            ),
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
            "Additional photographs of a person. Photos flagged with "
            "use_for_face_recognition are used as training examples for "
            "the local face-recognition model."
        ),
    )
    op.create_index(
        "ix_person_photos_person_id", "person_photos", ["person_id"]
    )

    op.create_table(
        "person_relationships",
        sa.Column(
            "person_relationship_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "from_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="Subject of the edge. For parent_of this is the parent.",
        ),
        sa.Column(
            "to_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="Object of the edge. For parent_of this is the child.",
        ),
        sa.Column(
            "relationship_type",
            sa.String(length=20),
            nullable=False,
            comment=(
                "Edge type: 'parent_of' (directional) or 'spouse_of' "
                "(symmetric)."
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
        sa.UniqueConstraint(
            "from_person_id",
            "to_person_id",
            "relationship_type",
            name="uq_person_relationship_edge",
        ),
        sa.CheckConstraint(
            "from_person_id <> to_person_id",
            name="ck_person_relationship_not_self",
        ),
        sa.CheckConstraint(
            "relationship_type IN ('parent_of', 'spouse_of')",
            name="ck_person_relationship_type_valid",
        ),
        comment=(
            "The atomic edges of the family tree. Use parent_of for "
            "parent/child relationships (directional: from=parent, "
            "to=child) and spouse_of for marriages/partnerships "
            "(stored symmetrically as two rows). Siblings, "
            "grandparents, aunts/uncles, and cousins are derived."
        ),
    )
    op.create_index(
        "ix_person_relationships_from_person_id",
        "person_relationships",
        ["from_person_id"],
    )
    op.create_index(
        "ix_person_relationships_to_person_id",
        "person_relationships",
        ["to_person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_person_relationships_to_person_id", table_name="person_relationships")
    op.drop_index("ix_person_relationships_from_person_id", table_name="person_relationships")
    op.drop_table("person_relationships")

    op.drop_index("ix_person_photos_person_id", table_name="person_photos")
    op.drop_table("person_photos")

    op.alter_column(
        "people",
        "primary_family_relationship",
        new_column_name="relationship_to_head_of_household",
        existing_type=sa.String(length=40),
        existing_nullable=True,
    )
