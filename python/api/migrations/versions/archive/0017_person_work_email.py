"""Add ``people.work_email`` for the secondary (work) calendar.

Each person now has two optional email addresses:

* ``email_address``  — personal mailbox / Google Calendar
* ``work_email``     — work mailbox / employer-managed Google Calendar

The AI assistant resolves a person to BOTH calendars when checking
free/busy, listing events, or finding a free slot. Personal calendars
are typically shared with full event detail; work calendars are
usually shared as free/busy only. The relationship-based privacy
gate decides whether the speaker is allowed to see event titles or
just the busy intervals.

This migration is purely additive — no constraints, no backfill, no
unique index (people often share a household work email or have
identical-cased addresses across personal/work for one mailbox).

Revision ID: 0017_person_work_email
Revises: 0016_email_inbox
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017_person_work_email"
down_revision: Union[str, None] = "0016_email_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "people",
        sa.Column(
            "work_email",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Work / employer email address. Used by the AI assistant "
                "as a SECOND Google Calendar id when checking availability "
                "or listing events for this person — work calendars are "
                "typically only shared as free/busy, while personal "
                "calendars are full-detail. Optional."
            ),
        ),
    )
    # Lower-case index so the email-inbox poller's case-insensitive
    # lookup against work_email is as fast as the existing one against
    # email_address.
    op.create_index(
        "ix_people_work_email_lower",
        "people",
        [sa.text("lower(work_email)")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_people_work_email_lower", table_name="people")
    op.drop_column("people", "work_email")
