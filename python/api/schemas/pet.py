from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class PetBase(BaseModel):
    pet_name: str = Field(..., max_length=120)
    animal_type: str = Field(..., max_length=60)
    breed: Optional[str] = Field(None, max_length=120)
    color: Optional[str] = Field(None, max_length=60)
    date_of_birth: Optional[date] = None
    notes: Optional[str] = None


class PetCreate(PetBase):
    family_id: int


class PetUpdate(BaseModel):
    pet_name: Optional[str] = Field(None, max_length=120)
    animal_type: Optional[str] = Field(None, max_length=60)
    breed: Optional[str] = Field(None, max_length=120)
    color: Optional[str] = Field(None, max_length=60)
    date_of_birth: Optional[date] = None
    notes: Optional[str] = None


class PetRead(OrmModel):
    pet_id: int
    family_id: int
    pet_name: str
    animal_type: str
    breed: Optional[str]
    color: Optional[str]
    date_of_birth: Optional[date]
    notes: Optional[str]
    cover_photo_path: Optional[str] = None
