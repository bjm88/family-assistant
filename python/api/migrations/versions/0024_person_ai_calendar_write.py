"""Per-person consent flag for letting Avi write calendar events.

Adds ``people.ai_can_write_calendar`` (boolean, default false). When
true AND the household member's calendar is shared with the
assistant's Google account with edit permission, the new
``calendar_create_event`` tool is allowed to insert holds / events on
that calendar for the speaker.

Why per-person, why opt-in
--------------------------
Calendar writes are a higher-trust capability than calendar reads.
Even within a household we want each person to deliberately turn it
on for themselves, the same way they had to share their calendar
with Avi at the Google level. Defaulting to ``false`` means:

* Existing households see no behavioural change after migration.
* The tool surfaces a clear "you haven't given Avi permission to
  add events to your calendar — toggle it on under your profile" if
  someone tries to use it without flipping the switch first.
* The Google-side share permission and the in-app consent live in
  parallel: BOTH must be true for the write to succeed, so a
  forgotten share won't accidentally write to the wrong calendar
  and a forgotten in-app consent won't either.

Pure additive change — no backfill, no constraints, no index.
:func:`api.ai.tools._handle_calendar_create_event` reads the column
directly off the resolved ``Person`` row at call time.

Revision ID: 0024_person_ai_calendar_write
Revises: 0023_telegram_contact_verify
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0024_person_ai_calendar_write"
down_revision: Union[str, None] = "0023_telegram_contact_verify"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "people",
        sa.Column(
            "ai_can_write_calendar",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "When true, the AI assistant is permitted to add events "
                "(holds, reminders, blocks) to this person's personal "
                "Google calendar via the calendar_create_event tool. "
                "Requires the calendar to ALSO be shared with the "
                "assistant's Google account with edit permission — "
                "this flag is the in-app consent half of that pair. "
                "Defaults to false so existing households see no "
                "behaviour change after migration."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("people", "ai_can_write_calendar")
