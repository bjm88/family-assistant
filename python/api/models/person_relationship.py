"""The ``person_relationships`` table — the family tree.

We model only the minimal atomic edges needed to reconstruct any family
relationship by graph traversal:

* ``parent_of`` — directional. from_person is the parent, to_person is
  the child.
* ``spouse_of`` — symmetric. Stored as two rows (A→B and B→A) so both
  perspectives render identically without special-case query logic.

Derived relationships (siblings, grandparents, aunts/uncles, cousins,
in-laws, etc.) are computed at query time from these two primitives.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._mixins import TimestampMixin


RELATIONSHIP_TYPES = ("parent_of", "spouse_of")


class PersonRelationship(Base, TimestampMixin):
    __tablename__ = "person_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_person_id",
            "to_person_id",
            "relationship_type",
            name="uq_person_relationship_edge",
        ),
        CheckConstraint(
            "from_person_id <> to_person_id",
            name="ck_person_relationship_not_self",
        ),
        CheckConstraint(
            "relationship_type IN ('parent_of', 'spouse_of')",
            name="ck_person_relationship_type_valid",
        ),
        {
            "comment": (
                "The atomic edges of the family tree. Use parent_of for "
                "parent/child relationships (directional: from=parent, "
                "to=child) and spouse_of for marriages/partnerships "
                "(stored symmetrically as two rows). Siblings, "
                "grandparents, aunts/uncles, and cousins are derived."
            )
        },
    )

    person_relationship_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Subject of the edge. For parent_of this is the parent.",
    )
    to_person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Object of the edge. For parent_of this is the child.",
    )
    relationship_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Edge type: 'parent_of' (directional) or 'spouse_of' (symmetric).",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
