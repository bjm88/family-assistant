"""The ``sensitive_identifiers`` table — SSNs and other tax identifiers."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class SensitiveIdentifier(Base, TimestampMixin):
    __tablename__ = "sensitive_identifiers"
    __table_args__ = {
        "comment": (
            "Highly sensitive personal identifiers (SSN, ITIN, foreign tax "
            "IDs). The raw value is always encrypted; only the last four "
            "digits are stored in plaintext for display/search."
        )
    }

    sensitive_identifier_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    identifier_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of: social_security_number, itin, foreign_tax_id, other.",
    )
    identifier_value_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="Fernet-encrypted identifier value.",
    )
    identifier_last_four: Mapped[Optional[str]] = mapped_column(
        String(4),
        nullable=True,
        comment="Last four digits of the identifier. Safe for display.",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="sensitive_identifiers")  # noqa: F821
