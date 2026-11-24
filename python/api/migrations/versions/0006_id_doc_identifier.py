"""Plaintext identifier on identity documents.

Adds ``identity_documents.identifier`` for non-secret numbers that should
be shown in full (United MileagePlus, Known Traveler, …). The encrypted
``document_number_*`` columns are unchanged.

Revision ID: 0006_id_doc_identifier
Revises: 0005_goal_completed_date
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_id_doc_identifier"
down_revision: Union[str, None] = "0005_goal_completed_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "identity_documents",
        sa.Column(
            "identifier",
            sa.String(100),
            nullable=True,
            comment=(
                "Plaintext, non-secret identifier shown in full, e.g. a United "
                "MileagePlus or Known Traveler number. Secret numbers belong in "
                "document_number_encrypted instead."
            ),
        ),
    )
    op.alter_column(
        "identity_documents",
        "document_type",
        existing_type=sa.String(40),
        existing_nullable=False,
        comment=(
            "One of: drivers_license, passport, state_id, birth_certificate, "
            "permanent_resident_card, global_entry, military_id, "
            "united_mileage_plus, other."
        ),
    )


def downgrade() -> None:
    op.alter_column(
        "identity_documents",
        "document_type",
        existing_type=sa.String(40),
        existing_nullable=False,
        comment=(
            "One of: drivers_license, passport, state_id, birth_certificate, "
            "permanent_resident_card, global_entry, military_id, other."
        ),
    )
    op.drop_column("identity_documents", "identifier")
