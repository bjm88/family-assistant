"""The ``pet_photos`` table — additional photos of a pet.

Mirrors the ``person_photos`` design but without a face-recognition
flag. Each pet can have any number of tagged photos.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class PetPhoto(Base, TimestampMixin):
    __tablename__ = "pet_photos"
    __table_args__ = {
        "comment": (
            "Photographs of a pet. Each row references the stored file on "
            "disk relative to FA_STORAGE_ROOT and carries an optional "
            "human-readable title and description."
        )
    }

    pet_photo_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pet_id: Mapped[int] = mapped_column(
        ForeignKey("pets.pet_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Pet this photo belongs to.",
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Short human-readable name for the photo, e.g. "At the beach 2025".',
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

    pet: Mapped["Pet"] = relationship(back_populates="photos")  # noqa: F821
