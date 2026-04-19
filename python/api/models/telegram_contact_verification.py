"""``telegram_contact_verifications`` — SMS-based 2FA for Telegram contact share.

See ``migrations/versions/0023_telegram_contact_verify.py`` for the
full design rationale. The short version: the phone number that
arrives in a Telegram ``message.contact`` payload is supplied by the
sender's client, not signed by Telegram, so binding
``Person.telegram_user_id`` straight off it trusts a user-controlled
value. We close the gap by texting a one-time code to the matched
``Person.mobile_phone_number`` via Twilio and only completing the
bind once the user echoes it back into the bot chat — proving they
control BOTH the Telegram account AND the registered phone.

Code generation lives in :func:`generate_verification_code`; hashing
in :func:`hash_verification_code`. Cleartext is never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Default lifetime of a verification challenge. Long enough that the
# user has time to switch from Telegram → SMS app → back to Telegram,
# short enough that a stolen DB row can't be exploited a day later.
TELEGRAM_VERIFY_DEFAULT_TTL = timedelta(minutes=10)


# Default attempt budget. Six-digit codes have a 10**6 keyspace so
# 5 wrong guesses ≈ 5e-6 success rate per challenge — far below
# anything an attacker could reasonably exploit before the row
# auto-revokes.
TELEGRAM_VERIFY_DEFAULT_MAX_ATTEMPTS = 5


# Default code length in digits. Six is the universal SMS-2FA
# convention (Apple ID, Google, banks, …) so the user knows what
# shape of string to look for.
TELEGRAM_VERIFY_DEFAULT_CODE_LENGTH = 6


def generate_verification_code(*, length: int = TELEGRAM_VERIFY_DEFAULT_CODE_LENGTH) -> str:
    """Return a fresh zero-padded N-digit numeric verification code.

    Uses :func:`secrets.randbelow` so the result is suitable for a
    security-relevant challenge (not :mod:`random`'s Mersenne Twister
    which is predictable).
    """
    if length < 4 or length > 10:
        # 4 digits is the lower bound any 2FA flow should ever use,
        # 10 keeps us inside a single int. Outside that range almost
        # certainly indicates a misconfiguration we want to surface.
        raise ValueError(f"verification code length out of range: {length}")
    upper = 10**length
    n = secrets.randbelow(upper)
    return f"{n:0{length}d}"


def hash_verification_code(code: str) -> str:
    """Return the lowercase SHA-256 hex digest of ``code``.

    Used for both insertion (store the hash) and comparison
    (hash the user-supplied attempt and compare via
    :func:`hmac.compare_digest`).
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def verification_codes_match(*, expected_hash: str, provided_code: str) -> bool:
    """Constant-time comparison of a user-supplied code to a stored hash."""
    return hmac.compare_digest(expected_hash, hash_verification_code(provided_code))


class TelegramContactVerification(Base, TimestampMixin):
    __tablename__ = "telegram_contact_verifications"
    __table_args__ = (
        Index(
            "ix_telegram_contact_verify_family",
            "family_id",
        ),
        Index(
            "ix_telegram_contact_verify_person",
            "person_id",
        ),
        Index(
            "uq_telegram_contact_verify_active_per_chat",
            "telegram_chat_id",
            unique=True,
            postgresql_where=text(
                "claimed_at IS NULL AND revoked_at IS NULL"
            ),
        ),
        {
            "comment": (
                "Outstanding + historical SMS verification challenges "
                "for the Telegram contact-share auto-link flow. A row "
                "becomes claimed iff the user echoes the texted code "
                "back into Telegram inside the TTL/attempt budget; "
                "only then does Person.telegram_user_id get bound."
            )
        },
    )

    telegram_contact_verification_id: Mapped[int] = mapped_column(
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
            "Household member the verification will bind on success. "
            "Resolved by phone-match against Person.{mobile,home,work}"
            "_phone_number at challenge time."
        ),
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "Telegram numeric id of the requester. The same id must "
            "consume the code; otherwise a third party who learns the "
            "chat could race the legitimate user."
        ),
    )
    telegram_username: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment=(
            "Private-chat id the verification flow is happening in. "
            "Used as the partial-unique key — only one challenge per "
            "chat may be outstanding at a time."
        ),
    )
    phone_normalised: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "E.164 normalisation of the phone the contact share named, "
            "stored verbatim for audit."
        ),
    )
    code_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex digest of the verification code.",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=TELEGRAM_VERIFY_DEFAULT_MAX_ATTEMPTS,
        server_default=str(TELEGRAM_VERIFY_DEFAULT_MAX_ATTEMPTS),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    twilio_message_sid: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    family: Mapped["Family"] = relationship()  # noqa: F821
    person: Mapped["Person"] = relationship()  # noqa: F821

    def is_outstanding(self, *, now: Optional[datetime] = None) -> bool:
        """True iff the row is still claimable right now."""
        moment = now or datetime.now(timezone.utc)
        return (
            self.claimed_at is None
            and self.revoked_at is None
            and self.expires_at > moment
            and self.attempts < self.max_attempts
        )

    def attempts_remaining(self) -> int:
        """How many wrong guesses the user has left before auto-revoke."""
        return max(0, self.max_attempts - self.attempts)
