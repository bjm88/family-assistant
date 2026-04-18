"""The ``person_photos`` table — additional photos of a person.

Separate from the single ``people.profile_photo_path``. This table lets
the family manager upload as many tagged photos of a person as they like.
Any photo with ``use_for_face_recognition = true`` will be picked up by
the Avi assistant's face-recognition enrollment pipeline.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class PersonPhoto(Base, TimestampMixin):
    __tablename__ = "person_photos"
    __table_args__ = {
        "comment": (
            "Additional photographs of a person. Photos flagged with "
            "use_for_face_recognition are used as training examples for "
            "the local face-recognition model."
        )
    }

    person_photo_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Short human-readable name for the photo, e.g. "Family reunion 2024".',
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional free-form description of where / when the photo was taken.",
    )
    use_for_face_recognition: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment=(
            "If true, the photo is included in the face-recognition "
            "enrollment set for this person."
        ),
    )

    stored_file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT where the image is stored.",
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="photos")  # noqa: F821
