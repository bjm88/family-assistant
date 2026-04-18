from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class FamilyBase(BaseModel):
    family_name: str = Field(..., max_length=120)
    head_of_household_notes: Optional[str] = None


class FamilyCreate(FamilyBase):
    pass


class FamilyUpdate(BaseModel):
    family_name: Optional[str] = Field(None, max_length=120)
    head_of_household_notes: Optional[str] = None


class FamilyRead(OrmModel):
    family_id: int
    family_name: str
    head_of_household_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class FamilySummary(OrmModel):
    family_id: int
    family_name: str
    people_count: int
    vehicles_count: int
    insurance_policies_count: int
    financial_accounts_count: int
    documents_count: int
