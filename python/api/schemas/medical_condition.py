from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class MedicalConditionBase(BaseModel):
    condition_name: str = Field(..., max_length=200)
    icd10_code: Optional[str] = Field(None, max_length=10)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    description: Optional[str] = None


class MedicalConditionCreate(MedicalConditionBase):
    person_id: int


class MedicalConditionUpdate(BaseModel):
    condition_name: Optional[str] = Field(None, max_length=200)
    icd10_code: Optional[str] = Field(None, max_length=10)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    description: Optional[str] = None


class MedicalConditionRead(OrmModel):
    medical_condition_id: int
    person_id: int
    condition_name: str
    icd10_code: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]
    description: Optional[str]
