"""The ``documents`` table — general file attachments for people or the family."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = {
        "comment": (
            "Catch-all uploaded files: scanned passports, tax returns, "
            "wills, medical records, receipts, school forms. The binary "
            "blob itself lives on the local filesystem under "
            "FA_STORAGE_ROOT; this row is the searchable metadata."
        )
    }

    document_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="If set, the document is associated with a single person.",
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Human-readable title, e.g. "2024 federal tax return".',
    )
    document_category: Mapped[Optional[str]] = mapped_column(
        String(60),
        nullable=True,
        comment=(
            "Free-form category: tax, medical, legal, education, financial, "
            "identity_scan, insurance, receipt, warranty, other."
        ),
    )

    stored_file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Relative path under FA_STORAGE_ROOT where the file is stored.",
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="documents")  # noqa: F821
    person: Mapped[Optional["Person"]] = relationship(back_populates="documents")  # noqa: F821
