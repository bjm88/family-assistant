from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from ._base import OrmModel


class PhysicianBase(BaseModel):
    physician_name: str = Field(..., max_length=200)
    specialty: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = None
    phone_number: Optional[str] = Field(None, max_length=40)
    email_address: Optional[EmailStr] = None
    description: Optional[str] = None


class PhysicianCreate(PhysicianBase):
    person_id: int


class PhysicianUpdate(BaseModel):
    physician_name: Optional[str] = Field(None, max_length=200)
    specialty: Optional[str] = Field(None, max_length=120)
    address: Optional[str] = None
    phone_number: Optional[str] = Field(None, max_length=40)
    email_address: Optional[EmailStr] = None
    description: Optional[str] = None


class PhysicianRead(OrmModel):
    physician_id: int
    person_id: int
    physician_name: str
    specialty: Optional[str]
    address: Optional[str]
    phone_number: Optional[str]
    email_address: Optional[str]
    description: Optional[str]
