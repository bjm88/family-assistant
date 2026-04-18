"""The ``assistants`` table — the local AI helper (e.g. "Avi") for a family.

One assistant per family. The visual/personality descriptions are used
both to generate the avatar image via Gemini and (later) as the system
prompt for Avi's tool-use loop.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Assistant(Base, TimestampMixin):
    __tablename__ = "assistants"
    __table_args__ = (
        UniqueConstraint("family_id", name="uq_assistant_per_family"),
        {
            "comment": (
                "The local AI assistant persona for a family. Defaults to "
                "'Avi'. Visual/personality descriptions drive both the "
                "generated avatar and the future tool-use system prompt."
            )
        },
    )

    assistant_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        comment="Each family has at most one assistant persona.",
    )

    assistant_name: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="Avi",
        comment="Display name of the assistant. Defaults to 'Avi'.",
    )
    gender: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        comment=(
            "Preferred gender presentation of the assistant: 'male' or "
            "'female'. Used to steer the generated avatar and voice."
        ),
    )
    visual_description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form description of the assistant's appearance. Fed "
            "directly into the Gemini image prompt to produce the avatar."
        ),
    )
    personality_description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form description of the assistant's personality, tone, "
            "and conversational style. Will be used as part of the system "
            "prompt during live conversation."
        ),
    )
    profile_image_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Path, relative to FA_STORAGE_ROOT, of the most recent avatar "
            "generated for this assistant."
        ),
    )
    avatar_generation_note: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "When avatar generation failed on the last attempt, the error "
            "message is stored here so the admin UI can surface it. Null "
            "after a successful generation."
        ),
    )

    family: Mapped["Family"] = relationship(back_populates="assistant")  # noqa: F821
