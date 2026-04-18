"""The ``live_session_messages`` table — transcript of a live session.

One row per utterance (user or assistant) or system note. The ``meta``
JSONB column is deliberately schemaless so we can tack on metadata
without migrations: model name, RAG context preview, goal references,
future attachment descriptors, latency measurements, etc.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Valid role values kept as a module tuple so both the schema layer and
# data-validation helpers can reuse the same canonical list.
LIVE_SESSION_MESSAGE_ROLES: tuple[str, ...] = ("user", "assistant", "system")


class LiveSessionMessage(Base, TimestampMixin):
    __tablename__ = "live_session_messages"
    __table_args__ = {
        "comment": (
            "Transcript of a live session. One row per utterance or "
            "system note, ordered by created_at."
        )
    }

    live_session_message_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    live_session_id: Mapped[int] = mapped_column(
        ForeignKey("live_sessions.live_session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Session this message belongs to.",
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment=(
            "Speaker role: 'user' (a family member), 'assistant' (Avi), "
            "or 'system' (automated note, e.g. 'session started')."
        ),
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "For role='user', the person we attribute the message to "
            "(derived from the active face-recognition result). NULL "
            "for assistant/system messages."
        ),
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Plain-text message body (what was said or typed).",
    )
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Free-form structured context: model name, goal reference, "
            "RAG preview, latency, future attachment descriptors."
        ),
    )

    session: Mapped["LiveSession"] = relationship(  # noqa: F821
        back_populates="messages"
    )
    person: Mapped[Optional["Person"]] = relationship()  # noqa: F821
