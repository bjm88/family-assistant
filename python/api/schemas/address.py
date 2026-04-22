from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class AddressBase(BaseModel):
    label: str = Field(..., max_length=40)
    street_line_1: str = Field(..., max_length=200)
    street_line_2: Optional[str] = Field(None, max_length=200)
    city: str = Field(..., max_length=120)
    state_or_region: Optional[str] = Field(None, max_length=80)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: str = Field("United States", max_length=80)
    is_primary_residence: bool = False
    notes: Optional[str] = None
    person_id: Optional[int] = None


class AddressCreate(AddressBase):
    family_id: int


class AddressUpdate(BaseModel):
    label: Optional[str] = None
    street_line_1: Optional[str] = None
    street_line_2: Optional[str] = None
    city: Optional[str] = None
    state_or_region: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    is_primary_residence: Optional[bool] = None
    notes: Optional[str] = None
    person_id: Optional[int] = None


class AddressRead(OrmModel):
    address_id: int
    family_id: int
    person_id: Optional[int]
    label: str
    street_line_1: str
    street_line_2: Optional[str]
    city: str
    state_or_region: Optional[str]
    postal_code: Optional[str]
    country: str
    is_primary_residence: bool
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
