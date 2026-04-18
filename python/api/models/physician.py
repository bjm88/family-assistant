"""The ``physicians`` table — doctors and other clinicians for a person.

Each row is a *care relationship* — the same human physician treating
two siblings is two rows here, one per person. That keeps the model
trivial (no shared-physician join table) and matches the way the
admin UI is presented (a list under each person's medical record).
The implementation will dedupe by ``(npi_number, person_id)`` if/when
NPI lookups are added.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Physician(Base, TimestampMixin):
    __tablename__ = "physicians"
    __table_args__ = {
        "comment": (
            "Doctors and other clinicians a specific person sees. "
            "Stored per-person (not deduplicated across the family) "
            "so each medical record is self-contained and editable "
            "in isolation."
        ),
    }

    physician_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The patient this physician treats.",
    )

    physician_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Full name as the patient knows them, e.g. 'Dr. Sarah Patel'.",
    )
    specialty: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment=(
            "Medical specialty (Pediatrics, Cardiology, Family Medicine, "
            "Dermatology, etc.). Free-form text."
        ),
    )
    address: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Office address as a single block (street, city, state, "
            "zip). Free-form so it doesn't need to be parsed for "
            "structured-address rules — clinicians often have suite "
            "numbers, building names, etc."
        ),
    )
    phone_number: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment="Office or scheduling phone number.",
    )
    email_address: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Direct email or portal contact, if known.",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form notes — what the patient sees them for, when "
            "the relationship started, scheduling quirks, etc."
        ),
    )

    person: Mapped["Person"] = relationship(back_populates="physicians")  # noqa: F821
