"""The ``vehicles`` table — cars, trucks, motorcycles, boats, etc."""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, Integer, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Common high-level vehicle categories the admin UI shows in a dropdown.
# The DB column is free-form so unusual entries aren't rejected, but the
# overview dashboard filters on these canonical values (e.g. "car" for
# the daily-drivers gallery).
COMMON_VEHICLE_TYPES = (
    "car",
    "truck",
    "motorcycle",
    "boat",
    "atv",
    "rv",
    "airplane",
    "bicycle",
    "golf_cart",
    "tractor",
    "trailer",
    "other",
)


class Vehicle(Base, TimestampMixin):
    __tablename__ = "vehicles"
    __table_args__ = {
        "comment": (
            "Motor vehicles owned or leased by the family. VIN and license "
            "plate are encrypted; their last-four helper columns are safe "
            "to display and filter."
        )
    }

    vehicle_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    primary_driver_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Which family member typically drives this vehicle.",
    )
    residence_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("residences.residence_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Optional home base for the vehicle (e.g. the boat is "
            "parked at the lake cabin). NULL when the vehicle isn't "
            "tied to a specific residence."
        ),
    )

    vehicle_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="car",
        comment=(
            "High-level category: car, truck, motorcycle, boat, atv, rv, "
            "airplane, bicycle, golf_cart, tractor, trailer, other. "
            "Drives which entries appear in the overview gallery."
        ),
    )
    nickname: Mapped[Optional[str]] = mapped_column(
        String(60),
        nullable=True,
        comment='Friendly name, e.g. "The blue van".',
    )
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    make: Mapped[str] = mapped_column(String(60), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    trim: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    body_style: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment="sedan, suv, minivan, pickup, coupe, motorcycle, etc.",
    )
    fuel_type: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment="gasoline, diesel, hybrid, plug_in_hybrid, electric, etc.",
    )

    vehicle_identification_number_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary,
        nullable=True,
        comment="Fernet-encrypted 17-character VIN.",
    )
    vehicle_identification_number_last_four: Mapped[Optional[str]] = mapped_column(
        String(4), nullable=True
    )
    license_plate_number_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    license_plate_number_last_four: Mapped[Optional[str]] = mapped_column(
        String(4),
        nullable=True,
        comment="Last four characters of the plate. Safe for display.",
    )
    license_plate_state_or_region: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True
    )

    registration_expiration_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True
    )
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    purchase_price_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    current_mileage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    profile_image_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Filesystem path (relative to FA_STORAGE_ROOT) of the "
            "vehicle's profile picture. Optional; renders as a placeholder "
            "icon when absent."
        ),
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="vehicles")  # noqa: F821
