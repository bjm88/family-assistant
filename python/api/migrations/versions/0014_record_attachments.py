"""Files attached to insurance policies and vehicles.

Adds ``insurance_policy_attachments`` (photos of the insurance card,
PDFs of the policy) and ``vehicle_attachments`` (the title, the
registration). One row per stored photo or PDF.

Revision ID: 0014_record_attachments
Revises: 0013_slack_inbox
Create Date: 2026-11-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# alembic_version.version_num is VARCHAR(32) — keep revision ids short.
revision: str = "0014_record_attachments"
down_revision: Union[str, None] = "0013_slack_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _file_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "label",
            sa.String(200),
            nullable=True,
            comment='Optional short note saying which file this is, e.g. "Front" or "2026 renewal".',
        ),
        sa.Column(
            "stored_file_path",
            sa.String(500),
            nullable=False,
            comment="Path relative to FA_STORAGE_ROOT where the file is stored.",
        ),
        sa.Column(
            "original_file_name",
            sa.String(255),
            nullable=False,
            comment="File name the file had when it was uploaded.",
        ),
        sa.Column(
            "mime_type",
            sa.String(120),
            nullable=False,
            comment="application/pdf for a PDF, otherwise an image type such as image/jpeg.",
        ),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp when the row was first inserted.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp of the most recent update to the row.",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "insurance_policy_attachments",
        sa.Column(
            "insurance_policy_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "insurance_policy_id",
            sa.Integer(),
            sa.ForeignKey(
                "insurance_policies.insurance_policy_id", ondelete="CASCADE"
            ),
            nullable=False,
            comment="Policy this file belongs to.",
        ),
        sa.Column(
            "attachment_kind",
            sa.String(30),
            nullable=False,
            comment="One of: insurance_card, policy_document, other.",
        ),
        *_file_columns(),
        comment=(
            "Photos and PDFs attached to an insurance policy: pictures of "
            "the insurance card, the policy or declarations document. One "
            "row per file; the file itself is on disk under FA_STORAGE_ROOT."
        ),
    )
    op.create_index(
        "ix_insurance_policy_attachments_insurance_policy_id",
        "insurance_policy_attachments",
        ["insurance_policy_id"],
    )

    op.create_table(
        "vehicle_attachments",
        sa.Column(
            "vehicle_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.vehicle_id", ondelete="CASCADE"),
            nullable=False,
            comment="Vehicle this file belongs to.",
        ),
        sa.Column(
            "attachment_kind",
            sa.String(30),
            nullable=False,
            comment="One of: title, registration, other.",
        ),
        *_file_columns(),
        comment=(
            "Photos and PDFs of a vehicle's paperwork: the title "
            "(certificate of ownership), the registration, and similar. "
            "One row per file; the file itself is on disk under "
            "FA_STORAGE_ROOT. The vehicle's own picture is "
            "vehicles.profile_image_path, not a row here."
        ),
    )
    op.create_index(
        "ix_vehicle_attachments_vehicle_id", "vehicle_attachments", ["vehicle_id"]
    )


def downgrade() -> None:
    # Drops the rows only. The files they point at stay on disk under
    # family_<id>/insurance/ and family_<id>/vehicles/*/documents/.
    op.drop_index("ix_vehicle_attachments_vehicle_id", table_name="vehicle_attachments")
    op.drop_table("vehicle_attachments")
    op.drop_index(
        "ix_insurance_policy_attachments_insurance_policy_id",
        table_name="insurance_policy_attachments",
    )
    op.drop_table("insurance_policy_attachments")
