"""Add per-person medical record tables.

* ``medical_conditions`` — diagnoses with optional ICD-10 code, start /
  end date, and free-form description.
* ``medications`` — prescriptions / OTCs with NDC, generic, brand,
  dosage, start / end date, notes. CHECK constraint guarantees at
  least one of NDC / generic / brand is populated so each row is
  identifiable.
* ``physicians`` — care relationships (one row per (person, doctor))
  with name, specialty, free-form address, phone, email, notes.

All three tables hang directly off ``people.person_id`` with
``ON DELETE CASCADE`` so removing a family member cleans up their
medical record automatically.

Revision ID: 0013_medical_records
Revises: 0012_assistant_email
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_medical_records"
down_revision: Union[str, None] = "0012_assistant_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "medical_conditions",
        sa.Column(
            "medical_condition_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The person this diagnosis belongs to.",
        ),
        sa.Column(
            "condition_name",
            sa.String(length=200),
            nullable=False,
            comment=(
                "Human-readable name of the condition, e.g. 'Type 2 "
                "diabetes' or 'Seasonal allergies'."
            ),
        ),
        sa.Column(
            "icd10_code",
            sa.String(length=10),
            nullable=True,
            comment=(
                "ICD-10-CM diagnosis code (e.g. 'E11.9'). Optional — "
                "many household entries won't have one. Format is 1 "
                "letter + 2 digits + optional .digits, max 7 chars in "
                "practice; we allow 10 for forward-compat with longer "
                "extensions."
            ),
        ),
        sa.Column(
            "start_date",
            sa.Date(),
            nullable=True,
            comment=(
                "When the diagnosis was made / the condition started. "
                "The user-facing field is labelled 'start time' but the "
                "column is a calendar date — medical timing is rarely "
                "tracked to the minute and Postgres DATE plays nicest "
                "with the rest of the schema."
            ),
        ),
        sa.Column(
            "end_date",
            sa.Date(),
            nullable=True,
            comment=(
                "When the condition resolved / treatment ended. NULL "
                "means the condition is still active."
            ),
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form notes — symptoms, severity, triggers, "
                "treatment plan."
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
            "Medical diagnoses (past or present) for a specific "
            "person. Open conditions have end_date IS NULL; closed "
            "conditions retain end_date so we keep medical history "
            "without losing the timeline. icd10_code is an optional "
            "standard ICD-10-CM diagnosis code so external systems "
            "and the LLM can map to well-known nomenclature."
        ),
    )
    op.create_index(
        "ix_medical_conditions_person_id",
        "medical_conditions",
        ["person_id"],
    )

    op.create_table(
        "medications",
        sa.Column(
            "medication_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The person taking this medication.",
        ),
        sa.Column(
            "ndc_number",
            sa.String(length=20),
            nullable=True,
            comment=(
                "FDA National Drug Code — typically a 10- or 11-digit "
                "string with two hyphens (e.g. '0093-7146-01')."
            ),
        ),
        sa.Column(
            "generic_name",
            sa.String(length=160),
            nullable=True,
            comment=(
                "International Nonproprietary Name (e.g. 'ibuprofen'). "
                "Optional but strongly recommended."
            ),
        ),
        sa.Column(
            "brand_name",
            sa.String(length=160),
            nullable=True,
            comment="Manufacturer-marketed name (e.g. 'Advil', 'Tylenol PM').",
        ),
        sa.Column(
            "dosage",
            sa.String(length=120),
            nullable=True,
            comment=(
                "Free-form dose + frequency, e.g. '20mg once daily' "
                "or '1 tablet at bedtime'."
            ),
        ),
        sa.Column(
            "start_date",
            sa.Date(),
            nullable=True,
            comment="When the person started taking the medication.",
        ),
        sa.Column(
            "end_date",
            sa.Date(),
            nullable=True,
            comment=(
                "When the person stopped taking it. NULL means the "
                "medication is still active."
            ),
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form notes — prescriber, side effects, refill "
                "cadence, interactions to watch for."
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
        sa.CheckConstraint(
            "generic_name IS NOT NULL OR brand_name IS NOT NULL "
            "OR ndc_number IS NOT NULL",
            name="ck_medications_at_least_one_identifier",
        ),
        comment=(
            "Medications a specific person takes (or has taken). An "
            "active medication has end_date IS NULL. At least one of "
            "generic_name / brand_name / ndc_number must be populated "
            "so each row is identifiable."
        ),
    )
    op.create_index(
        "ix_medications_person_id",
        "medications",
        ["person_id"],
    )

    op.create_table(
        "physicians",
        sa.Column(
            "physician_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The patient this physician treats.",
        ),
        sa.Column(
            "physician_name",
            sa.String(length=200),
            nullable=False,
            comment="Full name as the patient knows them, e.g. 'Dr. Sarah Patel'.",
        ),
        sa.Column(
            "specialty",
            sa.String(length=120),
            nullable=True,
            comment=(
                "Medical specialty (Pediatrics, Cardiology, Family "
                "Medicine, Dermatology, etc.). Free-form text."
            ),
        ),
        sa.Column(
            "address",
            sa.Text(),
            nullable=True,
            comment=(
                "Office address as a single block (street, city, "
                "state, zip). Free-form so it doesn't need to be "
                "parsed for structured-address rules."
            ),
        ),
        sa.Column(
            "phone_number",
            sa.String(length=40),
            nullable=True,
            comment="Office or scheduling phone number.",
        ),
        sa.Column(
            "email_address",
            sa.String(length=255),
            nullable=True,
            comment="Direct email or portal contact, if known.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form notes — what the patient sees them for, "
                "when the relationship started, scheduling quirks, "
                "etc."
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
            "Doctors and other clinicians a specific person sees. "
            "Stored per-person (not deduplicated across the family) "
            "so each medical record is self-contained and editable "
            "in isolation."
        ),
    )
    op.create_index(
        "ix_physicians_person_id",
        "physicians",
        ["person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_physicians_person_id", table_name="physicians")
    op.drop_table("physicians")
    op.drop_index("ix_medications_person_id", table_name="medications")
    op.drop_table("medications")
    op.drop_index(
        "ix_medical_conditions_person_id",
        table_name="medical_conditions",
    )
    op.drop_table("medical_conditions")
