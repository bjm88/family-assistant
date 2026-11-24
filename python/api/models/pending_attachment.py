"""The ``pending_attachments`` table — files Avi asked the sender about."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._mixins import TimestampMixin


class PendingAttachment(Base, TimestampMixin):
    __tablename__ = "pending_attachments"
    __table_args__ = {
        "comment": (
            "Inbound attachments Avi could not file on his own (unknown "
            "holder, unclear subject, unreadable). Avi asked the sender; "
            "their next message is handed to the agent together with the "
            "file so the answer can be acted on."
        )
    }

    pending_attachment_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The sender Avi is waiting to hear back from.",
    )
    channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    stored_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Relative path under FA_STORAGE_ROOT of the inbound file.",
    )
    question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="What Avi found and what he asked, as shown to the agent.",
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the follow-up was handed to the agent; NULL = still waiting.",
    )
