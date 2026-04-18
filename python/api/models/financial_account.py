"""The ``financial_accounts`` table — bank accounts, credit cards, loans, etc."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, LargeBinary, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class FinancialAccount(Base, TimestampMixin):
    __tablename__ = "financial_accounts"
    __table_args__ = {
        "comment": (
            "Bank accounts, credit cards, brokerage accounts, retirement "
            "accounts, loans, and mortgages. Account and routing numbers "
            "are always encrypted; account_number_last_four is safe for "
            "display and LLM-generated SQL filters."
        )
    }

    financial_account_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    primary_holder_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="The person whose name the account is primarily under.",
    )

    account_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment=(
            "One of: checking, savings, money_market, certificate_of_deposit, "
            "credit_card, brokerage, retirement_401k, retirement_ira, "
            "retirement_roth_ira, college_529, loan_auto, loan_personal, "
            "loan_student, mortgage, heloc, other."
        ),
    )
    institution_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment='Bank or brokerage name, e.g. "Chase", "Fidelity".',
    )
    account_nickname: Mapped[Optional[str]] = mapped_column(
        String(80),
        nullable=True,
        comment='Friendly label, e.g. "Joint checking", "Kids 529".',
    )

    account_number_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="Fernet-encrypted full account number."
    )
    account_number_last_four: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    routing_number_encrypted: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary,
        nullable=True,
        comment="Fernet-encrypted ABA/routing number (U.S. checking/savings only).",
    )

    current_balance_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(14, 2),
        nullable=True,
        comment="Most recently recorded balance, for informational use only.",
    )
    credit_limit_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
        comment="Credit limit for credit cards and HELOCs.",
    )

    online_login_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment='e.g. "https://chase.com" — used by Avi to know where to log in.',
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    family: Mapped["Family"] = relationship(back_populates="financial_accounts")  # noqa: F821
