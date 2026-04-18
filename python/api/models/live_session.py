"""The ``live_sessions`` table — one row per live AI-assistant interaction.

A session begins when Avi either recognizes a face or receives a chat
message, and ends after 30 minutes of no activity (swept lazily on the
next session lookup and also exposed via a manual "end session" action).

Every session has:

* zero-or-more :class:`LiveSessionParticipant` rows — the people the
  camera has identified during the session, with a ``greeted_already``
  flag that prevents Avi from repeatedly saying "Hi <name>" as the same
  face drifts in and out of frame.
* zero-or-more :class:`LiveSessionMessage` rows — the transcript of the
  conversation (user utterances, assistant replies, system notes).

The ``last_activity_at`` column is what the idle sweep keys off; every
new participant or message bumps it, so a long multi-family interaction
stays open as long as anyone is still engaging with Avi.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Valid end_reason tokens; kept as a tuple + string column rather than a
# DB enum so we can add new reasons without a migration (e.g. 'evicted'
# when a future admin "force-end-everything" button is added).
LIVE_SESSION_END_REASONS: tuple[str, ...] = (
    "timeout",
    "manual",
    "superseded",
)


class LiveSession(Base, TimestampMixin):
    __tablename__ = "live_sessions"
    __table_args__ = {
        "comment": (
            "One row per continuous AI-assistant interaction with a "
            "family. Messages and participants hang off this row so the "
            "history view can replay the whole conversation."
        )
    }

    live_session_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Family that owns this live session.",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When Avi opened the session (first face or first chat).",
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "When the session closed. NULL while the session is still "
            "considered active by the idle-timeout sweeper."
        ),
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment=(
            "Updated on every new message or participant. Drives the "
            "30-minute inactivity auto-close."
        ),
    )
    start_context: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Short tag describing why the session was opened, e.g. "
            "'page_opened', 'face_recognized:5', 'chat_initiated'."
        ),
    )
    end_reason: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "One of 'timeout' (idle sweep), 'manual' (closed from UI), "
            "'superseded' (a newer session took over), or NULL while "
            "still active."
        ),
    )

    participants: Mapped[List["LiveSessionParticipant"]] = relationship(  # noqa: F821
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="LiveSessionParticipant.joined_at",
    )
    messages: Mapped[List["LiveSessionMessage"]] = relationship(  # noqa: F821
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="LiveSessionMessage.created_at",
    )
