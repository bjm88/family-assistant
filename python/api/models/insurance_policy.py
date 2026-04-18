"""Insurance policies and their links to covered people/vehicles."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import (
    Date,
    ForeignKey,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class InsurancePolicy(Base, TimestampMixin):
    __tablename__ = "insurance_policies"
    __table_args__ = {
        "comment": (
            "One row per insurance policy the family holds: auto, home, "
            "renters, health, dental, vision, life, umbrella, pet, etc. "
            "The policy number is encrypted; policy_number_last_four is "
            "safe for display and for LLM-generated filter predicates."
        )
    }

    insurance_policy_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    policy_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "One of: auto, home, renters, condo, health, dental, vision, "
            "life, disability, umbrella, pet, travel, other."
        ),
    )
    carrier_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment='Insurance company, e.g. "State Farm", "Geico", "Kaiser Permanente".',
    )
    plan_name: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment='Plan or product name, e.g. "Gold HMO 2000".',
    )

    policy_number_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="Fernet-encrypted policy number."
    )
    policy_number_last_four: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)

    premium_amount_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    premium_billing_frequency: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="monthly, quarterly, semi_annual, annual.",
    )
    deductible_amount_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    coverage_limit_amount_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 2), nullable=True
    )

    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)

    agent_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    agent_phone_number: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    agent_email_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="insurance_policies")  # noqa: F821
    covered_people: Mapped[List["InsurancePolicyPerson"]] = relationship(
        back_populates="insurance_policy", cascade="all, delete-orphan"
    )
    covered_vehicles: Mapped[List["InsurancePolicyVehicle"]] = relationship(
        back_populates="insurance_policy", cascade="all, delete-orphan"
    )


class InsurancePolicyPerson(Base):
    __tablename__ = "insurance_policy_people"
    __table_args__ = (
        UniqueConstraint(
            "insurance_policy_id", "person_id", name="uq_insurance_policy_people"
        ),
        {
            "comment": (
                "Which people are covered by which policies (junction table)."
            )
        },
    )

    insurance_policy_person_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    insurance_policy_id: Mapped[int] = mapped_column(
        ForeignKey("insurance_policies.insurance_policy_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coverage_role: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment="primary_holder, spouse, dependent, covered_driver, beneficiary.",
    )

    insurance_policy: Mapped["InsurancePolicy"] = relationship(back_populates="covered_people")


class InsurancePolicyVehicle(Base):
    __tablename__ = "insurance_policy_vehicles"
    __table_args__ = (
        UniqueConstraint(
            "insurance_policy_id", "vehicle_id", name="uq_insurance_policy_vehicles"
        ),
        {"comment": "Which vehicles are covered by which auto policies."},
    )

    insurance_policy_vehicle_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    insurance_policy_id: Mapped[int] = mapped_column(
        ForeignKey("insurance_policies.insurance_policy_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vehicle_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.vehicle_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    insurance_policy: Mapped["InsurancePolicy"] = relationship(back_populates="covered_vehicles")
