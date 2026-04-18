"""The ``pets`` table — household animals belonging to a family.

Kept deliberately simple: name, species (``animal_type``), optional
birthday and notes. The UI offers a dropdown of common species plus an
"other" option, but the column itself is free-form text so unusual pets
are not rejected.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Common options shown in the admin UI. Kept in Python for reference /
# potential reuse in LLM prompting; the DB column itself is free-form.
COMMON_PET_ANIMAL_TYPES = (
    "dog",
    "cat",
    "bird",
    "rabbit",
    "guinea_pig",
    "hamster",
    "mouse",
    "rat",
    "ferret",
    "turtle",
    "tortoise",
    "lizard",
    "snake",
    "fish",
    "frog",
    "chicken",
    "duck",
    "goose",
    "goat",
    "sheep",
    "ram",
    "pig",
    "cow",
    "horse",
    "donkey",
    "other",
)


class Pet(Base, TimestampMixin):
    __tablename__ = "pets"
    __table_args__ = {
        "comment": (
            "Pets owned by the family. animal_type is free-form text but "
            "the admin UI suggests common species (dog, cat, bird, etc.) "
            "plus an 'other' escape hatch."
        )
    }

    pet_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Household this pet belongs to.",
    )

    pet_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment='Name the family calls the pet, e.g. "Biscuit".',
    )
    animal_type: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        comment=(
            "Species of the pet, typically one of the COMMON_PET_ANIMAL_TYPES "
            "values (dog, cat, bird, rabbit, etc.) but any free-form text is "
            "accepted so uncommon pets are never rejected."
        ),
    )
    breed: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment='Breed or sub-species, e.g. "Golden Retriever" or "Tabby".',
    )
    color: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form notes, e.g. quirks, medical conditions, favorite treats.",
    )

    family: Mapped["Family"] = relationship(back_populates="pets")  # noqa: F821
    photos: Mapped[List["PetPhoto"]] = relationship(  # noqa: F821
        back_populates="pet", cascade="all, delete-orphan"
    )

    @property
    def cover_photo_path(self) -> Optional[str]:
        """Most-recent photo of the pet, if any — used for list thumbnails."""
        if not self.photos:
            return None
        return max(self.photos, key=lambda p: p.created_at).stored_file_path
