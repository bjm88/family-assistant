"""Add the ``assistants`` table — one AI persona per family.

Revision ID: 0003_assistants
Revises: 0002_family_tree_and_photos
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_assistants"
down_revision: Union[str, None] = "0002_family_tree_and_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistants",
        sa.Column("assistant_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Each family has at most one assistant persona.",
        ),
        sa.Column(
            "assistant_name",
            sa.String(length=80),
            nullable=False,
            server_default="Avi",
            comment="Display name of the assistant. Defaults to 'Avi'.",
        ),
        sa.Column(
            "gender",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Preferred gender presentation of the assistant: 'male' or "
                "'female'. Used to steer the generated avatar and voice."
            ),
        ),
        sa.Column(
            "visual_description",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form description of the assistant's appearance. Fed "
                "directly into the Gemini image prompt to produce the avatar."
            ),
        ),
        sa.Column(
            "personality_description",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form description of the assistant's personality, tone, "
                "and conversational style. Used as part of the system prompt "
                "during live conversation."
            ),
        ),
        sa.Column(
            "profile_image_path",
            sa.String(length=500),
            nullable=True,
            comment=(
                "Path, relative to FA_STORAGE_ROOT, of the most recent avatar "
                "generated for this assistant."
            ),
        ),
        sa.Column(
            "avatar_generation_note",
            sa.Text(),
            nullable=True,
            comment=(
                "When avatar generation failed on the last attempt, the error "
                "message is stored here so the admin UI can surface it."
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
        sa.UniqueConstraint("family_id", name="uq_assistant_per_family"),
        comment=(
            "The local AI assistant persona for a family. Defaults to 'Avi'. "
            "Visual/personality descriptions drive both the generated avatar "
            "and the future tool-use system prompt."
        ),
    )


def downgrade() -> None:
    op.drop_table("assistants")
