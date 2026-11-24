"""Track when a goal is completed.

Adds ``goals.completed_date``. NULL means the goal is still open; a
date marks it complete as of that day.

Revision ID: 0005_goal_completed_date
Revises: 0004_person_sms_opted_out
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_goal_completed_date"
down_revision: Union[str, None] = "0004_person_sms_opted_out"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "goals",
        sa.Column(
            "completed_date",
            sa.Date(),
            nullable=True,
            comment=(
                "Date the goal was completed. NULL means the goal is still "
                "open; Avi only nudges about open goals."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("goals", "completed_date")
