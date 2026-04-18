"""The ``addresses`` table — physical addresses for a family or a person."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Address(Base, TimestampMixin):
    __tablename__ = "addresses"
    __table_args__ = {
        "comment": (
            "Physical addresses. An address can belong to a family (home) or "
            "to a single person (e.g. a college dorm for a child)."
        )
    }

    address_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="If set, this address is specific to one person rather than the whole family.",
    )

    label: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment='Short label like "home", "vacation", "work", "college".',
    )
    street_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    street_line_2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state_or_region: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(80), nullable=False, default="United States")

    is_primary_residence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="addresses")  # noqa: F821
