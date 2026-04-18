"""The ``medical_conditions`` table — diagnoses tracked for a person.

A condition captures something the person has been diagnosed with, past
or present. Open conditions (no ``end_date``) are the ones Avi will
weigh when answering "is anyone in the household currently dealing with
…?"; closed conditions stay on file as medical history. The optional
``icd10_code`` lets the LLM (and any future EHR/insurance integration)
match against standard codings.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class MedicalCondition(Base, TimestampMixin):
    __tablename__ = "medical_conditions"
    __table_args__ = {
        "comment": (
            "Medical diagnoses (past or present) for a specific person. "
            "An open condition has end_date IS NULL; closed conditions "
            "retain end_date so we keep medical history without losing "
            "the timeline. icd10_code is an optional standard ICD-10-CM "
            "diagnosis code (e.g. 'E11.9' for type-2 diabetes without "
            "complications) so external systems and the LLM can map to "
            "well-known nomenclature."
        ),
    }

    medical_condition_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The person this diagnosis belongs to.",
    )

    condition_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment=(
            "Human-readable name of the condition, e.g. 'Type 2 "
            "diabetes' or 'Seasonal allergies'."
        ),
    )
    icd10_code: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        comment=(
            "ICD-10-CM diagnosis code (e.g. 'E11.9'). Optional — many "
            "household entries won't have one. Format is 1 letter + 2 "
            "digits + optional .digits, max 7 chars in practice; we "
            "allow 10 for forward-compat with longer extensions."
        ),
    )
    start_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "When the diagnosis was made / the condition started. The "
            "user-facing field is labelled 'start time' but the column "
            "is a calendar date — medical timing is rarely tracked to "
            "the minute and Postgres DATE plays nicest with the rest of "
            "the schema."
        ),
    )
    end_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "When the condition resolved / treatment ended. NULL means "
            "the condition is still active."
        ),
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form notes — symptoms, severity, triggers, treatment "
            "plan. Surface in Avi's RAG block when discussing the "
            "person's health."
        ),
    )

    person: Mapped["Person"] = relationship(  # noqa: F821
        back_populates="medical_conditions"
    )
