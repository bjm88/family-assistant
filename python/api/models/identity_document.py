"""The ``identity_documents`` table — driver's licenses, passports, etc."""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class IdentityDocument(Base, TimestampMixin):
    __tablename__ = "identity_documents"
    __table_args__ = {
        "comment": (
            "Government-issued identity documents belonging to a person: "
            "driver's license, passport, state ID, birth certificate, "
            "permanent resident card, etc. The document number is stored "
            "encrypted; document_number_last_four is safe to display and "
            "to search on."
        )
    }

    identity_document_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    document_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "One of: drivers_license, passport, state_id, birth_certificate, "
            "permanent_resident_card, global_entry, military_id, other."
        ),
    )
    document_number_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary,
        nullable=True,
        comment="Fernet-encrypted document number. Decrypt only in the app layer.",
    )
    document_number_last_four: Mapped[Optional[str]] = mapped_column(
        String(4),
        nullable=True,
        comment="Last four characters of the document number. Safe for display.",
    )

    issuing_authority: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment='e.g. "California DMV", "U.S. Department of State".',
    )
    country_of_issue: Mapped[str] = mapped_column(
        String(80), nullable=False, default="United States"
    )
    state_or_region_of_issue: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        index=True,
        comment="Used by Avi to proactively warn about upcoming expirations.",
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    front_image_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Path, relative to FA_STORAGE_ROOT, of a scan or photo of the "
            "front of the document (e.g. license face, passport photo page)."
        ),
    )
    back_image_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Path, relative to FA_STORAGE_ROOT, of a scan or photo of the "
            "back of the document, when applicable."
        ),
    )

    person: Mapped["Person"] = relationship(back_populates="identity_documents")  # noqa: F821
