"""Add ``goals`` (per person) and ``pets`` (per family) tables.

Revision ID: 0005_goals_and_pets
Revises: 0004_identity_document_images
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_goals_and_pets"
down_revision: Union[str, None] = "0004_identity_document_images"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("goal_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The person this goal belongs to.",
        ),
        sa.Column(
            "goal_name",
            sa.String(length=200),
            nullable=False,
            comment='Short title of the goal, e.g. "Run a half-marathon".',
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment=(
                "Longer explanation of the goal, why it matters, and what "
                "success looks like."
            ),
        ),
        sa.Column(
            "start_date",
            sa.Date(),
            nullable=True,
            comment="When the person committed to working on the goal.",
        ),
        sa.Column(
            "priority",
            sa.String(length=20),
            nullable=False,
            server_default="normal",
            comment=(
                "One of: urgent, semi_urgent, normal, low. Drives how "
                "often Avi surfaces the goal in daily summaries."
            ),
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
        sa.CheckConstraint(
            "priority IN ('urgent', 'semi_urgent', 'normal', 'low')",
            name="ck_goals_priority_valid",
        ),
        comment=(
            "Personal goals belonging to a specific person. Priority "
            "ranks how important the goal is (urgent > semi_urgent > "
            "normal > low) so Avi can focus daily check-ins on what "
            "matters most."
        ),
    )
    op.create_index("ix_goals_person_id", "goals", ["person_id"])

    op.create_table(
        "pets",
        sa.Column("pet_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Household this pet belongs to.",
        ),
        sa.Column(
            "pet_name",
            sa.String(length=120),
            nullable=False,
            comment='Name the family calls the pet, e.g. "Biscuit".',
        ),
        sa.Column(
            "animal_type",
            sa.String(length=60),
            nullable=False,
            comment=(
                "Species of the pet. Typically one of the common options "
                "(dog, cat, bird, rabbit, guinea_pig, hamster, mouse, "
                "rat, ferret, turtle, tortoise, lizard, snake, fish, "
                "frog, chicken, duck, goose, goat, sheep, ram, pig, cow, "
                "horse, donkey) but any free-form text is accepted so "
                "uncommon pets are never rejected."
            ),
        ),
        sa.Column(
            "breed",
            sa.String(length=120),
            nullable=True,
            comment='Breed or sub-species, e.g. "Golden Retriever".',
        ),
        sa.Column("color", sa.String(length=60), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form notes, e.g. quirks, medical conditions, "
                "favorite treats."
            ),
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
            "Pets owned by the family. animal_type is free-form text but "
            "the admin UI suggests common species (dog, cat, bird, etc.) "
            "plus an 'other' escape hatch."
        ),
    )
    op.create_index("ix_pets_family_id", "pets", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_pets_family_id", table_name="pets")
    op.drop_table("pets")
    op.drop_index("ix_goals_person_id", table_name="goals")
    op.drop_table("goals")
