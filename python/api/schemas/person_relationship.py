from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel

RelationshipType = Literal["parent_of", "spouse_of"]


class PersonRelationshipCreate(BaseModel):
    from_person_id: int = Field(
        ...,
        description=(
            "For parent_of this is the parent. For spouse_of it is one of "
            "the partners (the other row is created automatically)."
        ),
    )
    to_person_id: int = Field(
        ...,
        description="For parent_of this is the child.",
    )
    relationship_type: RelationshipType
    notes: Optional[str] = None


class PersonRelationshipRead(OrmModel):
    person_relationship_id: int
    from_person_id: int
    to_person_id: int
    relationship_type: str
    notes: Optional[str]
