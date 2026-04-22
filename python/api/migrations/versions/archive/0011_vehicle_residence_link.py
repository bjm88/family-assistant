"""Link vehicles to a home base ``residence_id``.

Vehicles can optionally be parked at a specific residence (the lake cabin
boat, the work truck at the city apartment, …). Nullable so existing rows
keep working; ``ON DELETE SET NULL`` so deleting a residence doesn't take
the cars with it.

Revision ID: 0011_vehicle_residence_link
Revises: 0010_person_and_vehicle_tweaks
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_vehicle_residence_link"
down_revision: Union[str, None] = "0010_person_and_vehicle_tweaks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vehicles",
        sa.Column(
            "residence_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Optional home base for the vehicle (e.g. the boat is "
                "parked at the lake cabin). NULL when the vehicle isn't "
                "tied to a specific residence."
            ),
        ),
    )
    op.create_foreign_key(
        "fk_vehicles_residence_id",
        source_table="vehicles",
        referent_table="residences",
        local_cols=["residence_id"],
        remote_cols=["residence_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_vehicles_residence_id",
        "vehicles",
        ["residence_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_vehicles_residence_id", table_name="vehicles")
    op.drop_constraint("fk_vehicles_residence_id", "vehicles", type_="foreignkey")
    op.drop_column("vehicles", "residence_id")
