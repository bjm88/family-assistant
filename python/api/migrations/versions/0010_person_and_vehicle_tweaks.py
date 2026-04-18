"""Add ``people.interests_and_activities`` plus ``vehicles.vehicle_type`` and
``vehicles.profile_image_path``.

Three small product tweaks bundled into one migration so we don't end up
with three trivial revisions:

* People now have a free-form ``interests_and_activities`` field for
  hobbies, sports, instruments, fandoms — anything Avi can use as
  conversational context. Lives next to ``notes`` but is intentionally
  named so the LLM can distinguish "things they like / do" from
  "everything else".
* Vehicles get a ``vehicle_type`` discriminator (car, truck, boat, RV,
  motorcycle, ATV, airplane, …) so the overview page can show only the
  daily-drivers and not, say, the fishing boat. Defaults to ``car`` for
  every existing row to match what the admin form will land on.
* Vehicles get a ``profile_image_path`` mirroring people / assistants
  so the dashboard car gallery has something nice to render.

Revision ID: 0010_person_and_vehicle_tweaks
Revises: 0009_live_sessions
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_person_and_vehicle_tweaks"
down_revision: Union[str, None] = "0009_live_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "people",
        sa.Column(
            "interests_and_activities",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form text describing the person's hobbies, sports, "
                "clubs, instruments, fandoms, and other recurring activities. "
                "Read by the AI assistant to seed conversational follow-ups."
            ),
        ),
    )

    op.add_column(
        "vehicles",
        sa.Column(
            "vehicle_type",
            sa.String(length=40),
            nullable=False,
            server_default="car",
            comment=(
                "High-level vehicle category: car, truck, motorcycle, boat, "
                "atv, rv, airplane, bicycle, golf_cart, tractor, trailer, "
                "other. Drives which entries appear in the overview gallery."
            ),
        ),
    )
    op.add_column(
        "vehicles",
        sa.Column(
            "profile_image_path",
            sa.String(length=500),
            nullable=True,
            comment=(
                "Filesystem path (relative to FA_STORAGE_ROOT) of the "
                "vehicle's profile picture. Optional; renders as a placeholder "
                "icon when absent."
            ),
        ),
    )
    # Drop the server_default once existing rows are backfilled — new
    # inserts go through the ORM which sets the column explicitly.
    op.alter_column("vehicles", "vehicle_type", server_default=None)


def downgrade() -> None:
    op.drop_column("vehicles", "profile_image_path")
    op.drop_column("vehicles", "vehicle_type")
    op.drop_column("people", "interests_and_activities")
