"""The ``residences`` table — physical homes belonging to a family.

A residence is a richer, photo-backed concept than a plain address. A
family can have many residences (main house, cabin, rental) and exactly
one of them should be flagged as the primary residence.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Residence(Base, TimestampMixin):
    __tablename__ = "residences"
    __table_args__ = {
        "comment": (
            "Physical homes belonging to a family (main house, cabin, "
            "pied-à-terre, rental). Distinct from the legacy addresses "
            "table because residences carry photos and a primary-home "
            "flag. Exactly one residence per family should have "
            "is_primary_residence=true; the residences router enforces "
            "this automatically on create/update."
        )
    }

    residence_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Household this residence belongs to.",
    )

    label: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment='Short nickname, e.g. "Main house", "Lake cabin", "NYC apartment".',
    )
    street_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    street_line_2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state_or_region: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(
        String(80), nullable=False, default="United States"
    )

    is_primary_residence: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment=(
            "True for the family's primary residence. The residences "
            "router guarantees at most one row per family has this set."
        ),
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="residences")  # noqa: F821
    photos: Mapped[List["ResidencePhoto"]] = relationship(  # noqa: F821
        back_populates="residence", cascade="all, delete-orphan"
    )

    @property
    def cover_photo_path(self) -> Optional[str]:
        """Most-recent photo of the residence, if any — used for thumbnails."""
        if not self.photos:
            return None
        return max(self.photos, key=lambda p: p.created_at).stored_file_path
