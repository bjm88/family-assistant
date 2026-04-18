"""Add ``email_address`` to ``assistants``.

Avi will use this Gmail / Google Workspace address as the identity
for sending mail and reading the family's shared calendar (free/busy
lookups, upcoming events). Nullable so existing rows keep working
unchanged; the email/calendar tooling is just disabled when blank.

Revision ID: 0012_assistant_email
Revises: 0011_vehicle_residence_link
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_assistant_email"
down_revision: Union[str, None] = "0011_vehicle_residence_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "email_address",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Gmail / Google Workspace address that owns the assistant's "
                "inbox and calendar. The Gmail and Google Calendar APIs use "
                "this address to send mail on Avi's behalf and to read the "
                "shared family calendars (free/busy lookups, upcoming events). "
                "Leave blank if the assistant doesn't have its own mailbox yet."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "email_address")
