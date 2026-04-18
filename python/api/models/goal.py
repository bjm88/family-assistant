"""The ``goals`` table — personal goals owned by a specific person.

A goal captures something the family member is working toward (health,
financial, educational, behavioral, etc.). Avi will later reference open
goals during daily planning and nudge the person about progress.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


GOAL_PRIORITIES = ("urgent", "semi_urgent", "normal", "low")


class Goal(Base, TimestampMixin):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('urgent', 'semi_urgent', 'normal', 'low')",
            name="ck_goals_priority_valid",
        ),
        {
            "comment": (
                "Personal goals belonging to a specific person. Priority "
                "ranks how important the goal is (urgent > semi_urgent > "
                "normal > low) so Avi can focus daily check-ins on what "
                "matters most."
            )
        },
    )

    goal_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The person this goal belongs to.",
    )

    goal_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Short title of the goal, e.g. "Run a half-marathon".',
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Longer explanation of the goal, why it matters, and what success looks like.",
    )
    start_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment="When the person committed to working on the goal.",
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="normal",
        comment=(
            "One of: urgent, semi_urgent, normal, low. Drives how often "
            "Avi surfaces the goal in daily summaries."
        ),
    )

    person: Mapped["Person"] = relationship(back_populates="goals")  # noqa: F821
