"""The ``live_session_participants`` table — who Avi spoke with.

One row per ``(session, person)`` pair. ``greeted_already`` is the key
column: it starts ``False`` when the camera first identifies a person,
and flips to ``True`` atomically inside the ``/greet`` endpoint the
first time Avi says "Hi <name>". Any subsequent recognition of the
same person during the same session short-circuits the greeting.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class LiveSessionParticipant(Base, TimestampMixin):
    __tablename__ = "live_session_participants"
    __table_args__ = (
        UniqueConstraint(
            "live_session_id",
            "person_id",
            name="uq_live_session_participant",
        ),
        {
            "comment": (
                "Join table: one row per (session, person). "
                "``greeted_already`` gates repeat greetings within the "
                "same window."
            )
        },
    )

    live_session_participant_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    live_session_id: Mapped[int] = mapped_column(
        ForeignKey("live_sessions.live_session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Session this participant is part of.",
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        comment="The recognized family member.",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="First moment the camera matched this person in the session.",
    )
    greeted_already: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "False until Avi has said 'Hi <name>' to this person in this "
            "session. Flipped atomically by /greet to prevent repeat "
            "greetings as the face drifts in and out of view."
        ),
    )

    session: Mapped["LiveSession"] = relationship(  # noqa: F821
        back_populates="participants"
    )
    person: Mapped["Person"] = relationship()  # noqa: F821
