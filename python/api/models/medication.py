"""The ``medications`` table — drugs a person takes (or has taken).

Both ``generic_name`` and ``brand_name`` are optional because some
entries are easier to remember one way than the other (e.g. the user
might know "Advil" but not "ibuprofen", or vice versa). The
:class:`Medication` row will refuse to be saved when both are blank
*and* the NDC number is also blank — at that point we have nothing to
identify the drug at all. The check is enforced at the database level
via ``ck_medications_at_least_one_identifier``.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Medication(Base, TimestampMixin):
    __tablename__ = "medications"
    __table_args__ = (
        CheckConstraint(
            "generic_name IS NOT NULL OR brand_name IS NOT NULL "
            "OR ndc_number IS NOT NULL",
            name="ck_medications_at_least_one_identifier",
        ),
        {
            "comment": (
                "Medications a specific person takes (or has taken). "
                "An active medication has end_date IS NULL. At least "
                "one of generic_name / brand_name / ndc_number must be "
                "populated so each row is identifiable."
            ),
        },
    )

    medication_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The person taking this medication.",
    )

    ndc_number: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment=(
            "FDA National Drug Code — typically a 10- or 11-digit string "
            "with two hyphens (e.g. '0093-7146-01'). Stored verbatim "
            "as the user enters it; up to 20 chars to allow either "
            "the 10/11-digit hyphenated form or an unhyphenated string."
        ),
    )
    generic_name: Mapped[Optional[str]] = mapped_column(
        String(160),
        nullable=True,
        comment=(
            "International Nonproprietary Name (e.g. 'ibuprofen'). "
            "Optional but strongly recommended."
        ),
    )
    brand_name: Mapped[Optional[str]] = mapped_column(
        String(160),
        nullable=True,
        comment="Manufacturer-marketed name (e.g. 'Advil', 'Tylenol PM').",
    )
    dosage: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment=(
            "Free-form dose + frequency, e.g. '20mg once daily' or "
            "'1 tablet at bedtime'. Optional."
        ),
    )
    start_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment="When the person started taking the medication.",
    )
    end_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "When the person stopped taking it. NULL means the "
            "medication is still active."
        ),
    )
    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form notes — prescriber, side effects, refill cadence, "
            "interactions to watch for."
        ),
    )

    person: Mapped["Person"] = relationship(  # noqa: F821
        back_populates="medications"
    )
