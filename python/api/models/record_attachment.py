"""Files attached to an insurance policy or a vehicle.

``insurance_policy_attachments`` holds photos of insurance cards and
PDFs of policy documents. ``vehicle_attachments`` holds a photo or scan
of the vehicle's title, its registration, and similar paperwork.

The two tables have the same shape: which record the file belongs to,
what kind of paperwork it is, an optional label, and where the file is
stored. A file is a photo or a PDF.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# What the admin UI offers for ``attachment_kind``; the first is the default.
INSURANCE_POLICY_ATTACHMENT_KINDS = ("insurance_card", "policy_document", "other")
VEHICLE_ATTACHMENT_KINDS = ("title", "registration", "other")


class _StoredFileColumns:
    """Columns describing the stored file, shared by both tables."""

    label: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment='Optional short note saying which file this is, e.g. "Front" or "2026 renewal".',
    )
    stored_file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Path relative to FA_STORAGE_ROOT where the file is stored.",
    )
    original_file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="File name the file had when it was uploaded.",
    )
    mime_type: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="application/pdf for a PDF, otherwise an image type such as image/jpeg.",
    )
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")


class InsurancePolicyAttachment(_StoredFileColumns, Base, TimestampMixin):
    __tablename__ = "insurance_policy_attachments"
    __table_args__ = {
        "comment": (
            "Photos and PDFs attached to an insurance policy: pictures of "
            "the insurance card, the policy or declarations document. One "
            "row per file; the file itself is on disk under FA_STORAGE_ROOT."
        )
    }

    insurance_policy_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    insurance_policy_id: Mapped[int] = mapped_column(
        ForeignKey("insurance_policies.insurance_policy_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Policy this file belongs to.",
    )
    attachment_kind: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="One of: insurance_card, policy_document, other.",
    )

    insurance_policy: Mapped["InsurancePolicy"] = relationship(  # noqa: F821
        back_populates="attachments"
    )


class VehicleAttachment(_StoredFileColumns, Base, TimestampMixin):
    __tablename__ = "vehicle_attachments"
    __table_args__ = {
        "comment": (
            "Photos and PDFs of a vehicle's paperwork: the title "
            "(certificate of ownership), the registration, and similar. "
            "One row per file; the file itself is on disk under "
            "FA_STORAGE_ROOT. The vehicle's own picture is "
            "vehicles.profile_image_path, not a row here."
        )
    }

    vehicle_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.vehicle_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Vehicle this file belongs to.",
    )
    attachment_kind: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="One of: title, registration, other.",
    )

    vehicle: Mapped["Vehicle"] = relationship(back_populates="attachments")  # noqa: F821
