from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class SensitiveIdentifierBase(BaseModel):
    identifier_type: str = Field(..., max_length=32)
    notes: Optional[str] = None


class SensitiveIdentifierCreate(SensitiveIdentifierBase):
    person_id: int
    identifier_value: str = Field(..., description="Plaintext value; encrypted at rest.")


class SensitiveIdentifierUpdate(BaseModel):
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None
    notes: Optional[str] = None


class SensitiveIdentifierRead(OrmModel):
    sensitive_identifier_id: int
    person_id: int
    identifier_type: str
    identifier_last_four: Optional[str]
    notes: Optional[str]
