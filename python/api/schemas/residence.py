from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class ResidenceBase(BaseModel):
    label: str = Field(..., max_length=80)
    street_line_1: str = Field(..., max_length=200)
    street_line_2: Optional[str] = Field(None, max_length=200)
    city: str = Field(..., max_length=120)
    state_or_region: Optional[str] = Field(None, max_length=80)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: str = Field("United States", max_length=80)
    is_primary_residence: bool = False
    notes: Optional[str] = None


class ResidenceCreate(ResidenceBase):
    family_id: int


class ResidenceUpdate(BaseModel):
    label: Optional[str] = Field(None, max_length=80)
    street_line_1: Optional[str] = Field(None, max_length=200)
    street_line_2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=120)
    state_or_region: Optional[str] = Field(None, max_length=80)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=80)
    is_primary_residence: Optional[bool] = None
    notes: Optional[str] = None


class ResidenceRead(OrmModel):
    residence_id: int
    family_id: int
    label: str
    street_line_1: str
    street_line_2: Optional[str]
    city: str
    state_or_region: Optional[str]
    postal_code: Optional[str]
    country: str
    is_primary_residence: bool
    notes: Optional[str]
    cover_photo_path: Optional[str] = None
