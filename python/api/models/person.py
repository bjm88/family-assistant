"""The ``people`` table — individual family members."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Person(Base, TimestampMixin):
    __tablename__ = "people"
    __table_args__ = {
        "comment": (
            "One row per person in the household (parents, children, "
            "extended family, regular caregivers). The family's head of "
            "household is stored in this table like anyone else and "
            "pointed at by families.head_of_household_person_id if you "
            "add that column later."
        )
    }

    person_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Household this person belongs to.",
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    middle_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    preferred_name: Mapped[Optional[str]] = mapped_column(
        String(80),
        nullable=True,
        comment='Nickname used in conversation, e.g. "Katie" for Katherine.',
    )

    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment="Self-identified gender (free-text, e.g. female/male/nonbinary).",
    )
    primary_family_relationship: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment=(
            "Primary relationship label from the perspective of the family "
            "manager (self, spouse, parent, child, sibling, guardian, "
            "grandparent, grandchild, etc.). This is a convenience label "
            "only; the authoritative family tree lives in "
            "person_relationships."
        ),
    )

    email_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mobile_phone_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    home_phone_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    work_phone_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    profile_photo_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Filesystem path (relative to FA_STORAGE_ROOT) of the person's "
            "current profile photo. Also used as a training anchor for face "
            "recognition by the Avi assistant."
        ),
    )

    interests_and_activities: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form list of hobbies, sports, clubs, instruments, "
            "fandoms, and other recurring activities. Used by the AI "
            "assistant to seed conversational follow-ups."
        ),
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="people")  # noqa: F821
    sensitive_identifiers: Mapped[List["SensitiveIdentifier"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    identity_documents: Mapped[List["IdentityDocument"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    documents: Mapped[List["Document"]] = relationship(  # noqa: F821
        back_populates="person"
    )
    photos: Mapped[List["PersonPhoto"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    goals: Mapped[List["Goal"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    medical_conditions: Mapped[List["MedicalCondition"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    medications: Mapped[List["Medication"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
    physicians: Mapped[List["Physician"]] = relationship(  # noqa: F821
        back_populates="person", cascade="all, delete-orphan"
    )
