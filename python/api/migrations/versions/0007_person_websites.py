"""Add ``person_websites`` for named URLs belonging to a person.

Each household member can now have zero, one, or many websites
(LinkedIn, personal blog, GitHub, school portal, …). Capturing a
separate row per (name, url) pair keeps the admin UI list-shaped
and lets the AI assistant cite a specific named link.

Revision ID: 0007_person_websites
Revises: 0006_id_doc_identifier
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_person_websites"
down_revision: Union[str, None] = "0006_id_doc_identifier"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "person_websites",
        sa.Column(
            "person_website_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The person this website belongs to.",
        ),
        sa.Column(
            "name",
            sa.String(length=120),
            nullable=False,
            comment=(
                "Short label for the site as the household refers "
                "to it, e.g. 'LinkedIn', 'Personal site', 'GitHub'."
            ),
        ),
        sa.Column(
            "url",
            sa.String(length=500),
            nullable=False,
            comment=(
                "Website URL. Free-form text — store exactly what "
                "the user typed (with or without https://) so we "
                "don't accidentally normalise away paths or "
                "subdomains the household cares about."
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
            "Named websites belonging to a household member. One "
            "row per (name, url) pair — e.g. name='LinkedIn', "
            "url='https://linkedin.com/in/…'. A person can have "
            "many websites; nothing prevents two rows sharing a "
            "name if the household wants that."
        ),
    )
    op.create_index(
        "ix_person_websites_person_id",
        "person_websites",
        ["person_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_person_websites_person_id",
        table_name="person_websites",
    )
    op.drop_table("person_websites")
