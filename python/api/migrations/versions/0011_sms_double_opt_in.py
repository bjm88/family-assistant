"""SMS double opt-in: SUBSCRIBE → reply Y → enrolled.

Revision ID: 0011_sms_double_opt_in
Revises: 0010_open_thread_sessions
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_sms_double_opt_in"
down_revision: Union[str, None] = "0010_open_thread_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "people",
        sa.Column("sms_opt_in_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "people",
        sa.Column("sms_opted_in_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("people", "sms_opted_in_at")
    op.drop_column("people", "sms_opt_in_requested_at")
