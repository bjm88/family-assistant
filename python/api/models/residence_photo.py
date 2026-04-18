"""The ``residence_photos`` table — additional photos of a family home.

Mirrors the ``pet_photos`` / ``person_photos`` design. Each residence can
have any number of photos (exterior, interior, floor plans, etc.).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class ResidencePhoto(Base, TimestampMixin):
    __tablename__ = "residence_photos"
    __table_args__ = {
        "comment": (
            "Photographs of a residence. Each row references the stored file "
            "on disk relative to FA_STORAGE_ROOT and carries an optional "
            "human-readable title and description."
        )
    }

    residence_photo_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    residence_id: Mapped[int] = mapped_column(
        ForeignKey("residences.residence_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Residence this photo belongs to.",
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Short human-readable name for the photo, e.g. "Front yard".',
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional free-form description of where / when the photo was taken.",
    )

    stored_file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT where the image is stored.",
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    residence: Mapped["Residence"] = relationship(back_populates="photos")  # noqa: F821
