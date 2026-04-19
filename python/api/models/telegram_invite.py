"""``telegram_invites`` — outstanding + historical Telegram deep-link invites.

See ``migrations/versions/0021_telegram_invites.py`` for the table
docstring and design rationale. The short version: Telegram bots
cannot send the first message, so the agent generates a one-time
``t.me/<bot>?start=<token>`` URL and delivers it via SMS or email.
The recipient tapping the link arrives at the bot pre-loaded with
``/start <token>``; we look the token up here, copy the resulting
``telegram_user_id`` / ``telegram_username`` onto the ``people`` row,
and the standard security gate covers every subsequent message.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Keep in lock-step with the CHECK constraint in
# ``migrations/versions/0021_telegram_invites.py``.
TELEGRAM_INVITE_CHANNELS: tuple[str, ...] = ("sms", "email", "manual")


# Default invite lifetime. Picked deliberately on the long side so a
# busy family member who only checks SMS once a week still has time
# to act on the link.
TELEGRAM_INVITE_DEFAULT_TTL = timedelta(days=30)


def generate_invite_token(*, n_bytes: int = 24) -> str:
    """Return a URL-safe random payload suitable for the ``?start=`` arg.

    Telegram caps the deep-link payload at 64 chars, ``A-Za-z0-9_-``.
    ``secrets.token_urlsafe(24)`` produces 32 chars from that alphabet,
    which keeps us well under the ceiling and gives ~192 bits of
    entropy — far more than enough that a guessing attack is hopeless.
    """
    return secrets.token_urlsafe(n_bytes)


class TelegramInvite(Base, TimestampMixin):
    __tablename__ = "telegram_invites"
    __table_args__ = (
        UniqueConstraint(
            "payload_token",
            name="uq_telegram_invites_payload_token",
        ),
        CheckConstraint(
            "sent_via IN ('sms', 'email', 'manual')",
            name="ck_telegram_invites_sent_via",
        ),
        Index("ix_telegram_invites_family", "family_id"),
        Index("ix_telegram_invites_person", "person_id"),
        Index(
            "uq_telegram_invites_active_per_person",
            "person_id",
            unique=True,
            postgresql_where=text(
                "claimed_at IS NULL AND revoked_at IS NULL"
            ),
        ),
        {
            "comment": (
                "Outstanding + historical Telegram deep-link invites. "
                "The /start <payload> handler in services.telegram_inbox "
                "claims a row, copies the binding onto the people row, "
                "and from then on the standard person-lookup gate "
                "applies."
            )
        },
    )

    telegram_invite_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        comment=(
            "The household member the invite is FOR. Whoever first "
            "hits /start <payload> becomes this person's Telegram "
            "identity."
        ),
    )
    created_by_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Family member who triggered the invite (typically the "
            "speaker who asked Avi to send it)."
        ),
    )
    payload_token: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment=(
            "URL-safe random secret embedded in the deep link as the "
            "?start= parameter."
        ),
    )
    sent_via: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Channel used to deliver the link — 'sms', 'email', or 'manual'.",
    )
    sent_to: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Literal phone/email we delivered to (audit only).",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    claimed_telegram_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )
    claimed_telegram_username: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    family: Mapped["Family"] = relationship()  # noqa: F821
    person: Mapped["Person"] = relationship(  # noqa: F821
        foreign_keys=[person_id]
    )
    created_by: Mapped[Optional["Person"]] = relationship(  # noqa: F821
        foreign_keys=[created_by_person_id]
    )

    def is_outstanding(self, *, now: Optional[datetime] = None) -> bool:
        """Convenience: True iff the row is still claimable."""
        moment = now or datetime.now(timezone.utc)
        return (
            self.claimed_at is None
            and self.revoked_at is None
            and self.expires_at > moment
        )
