from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from ._base import OrmModel


class JobBase(BaseModel):
    company_name: Optional[str] = Field(None, max_length=200)
    company_website: Optional[str] = Field(None, max_length=500)
    role_title: Optional[str] = Field(None, max_length=160)
    work_email: Optional[EmailStr] = None
    description: Optional[str] = None


class JobCreate(JobBase):
    person_id: int


class JobUpdate(BaseModel):
    company_name: Optional[str] = Field(None, max_length=200)
    company_website: Optional[str] = Field(None, max_length=500)
    role_title: Optional[str] = Field(None, max_length=160)
    work_email: Optional[EmailStr] = None
    description: Optional[str] = None


class JobRead(OrmModel):
    job_id: int
    person_id: int
    company_name: Optional[str]
    company_website: Optional[str]
    role_title: Optional[str]
    work_email: Optional[str]
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
