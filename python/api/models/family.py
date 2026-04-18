"""The ``families`` table — the top-level tenant for all other records."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Family(Base, TimestampMixin):
    __tablename__ = "families"
    __table_args__ = {
        "comment": (
            "One row per household. Every other family-assistant record "
            "(people, vehicles, policies, accounts, documents) belongs to "
            "exactly one family."
        )
    }

    family_id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
        comment="Surrogate primary key for the family.",
    )
    family_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment='Display name for the household, e.g. "The Smith Family".',
    )
    head_of_household_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form notes about the family manager or household.",
    )

    people: Mapped[List["Person"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    vehicles: Mapped[List["Vehicle"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    insurance_policies: Mapped[List["InsurancePolicy"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    financial_accounts: Mapped[List["FinancialAccount"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    documents: Mapped[List["Document"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    addresses: Mapped[List["Address"]] = relationship(  # noqa: F821
        back_populates="family", cascade="all, delete-orphan"
    )
    assistant: Mapped[Optional["Assistant"]] = relationship(  # noqa: F821
        back_populates="family",
        cascade="all, delete-orphan",
        uselist=False,
    )
