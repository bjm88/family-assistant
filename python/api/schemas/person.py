from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from ._base import OrmModel


class PersonBase(BaseModel):
    first_name: str = Field(..., max_length=80)
    middle_name: Optional[str] = Field(None, max_length=80)
    last_name: str = Field(..., max_length=80)
    preferred_name: Optional[str] = Field(None, max_length=80)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, max_length=32)
    primary_family_relationship: Optional[str] = Field(None, max_length=40)
    email_address: Optional[EmailStr] = None
    work_email: Optional[EmailStr] = None
    mobile_phone_number: Optional[str] = Field(None, max_length=40)
    home_phone_number: Optional[str] = Field(None, max_length=40)
    work_phone_number: Optional[str] = Field(None, max_length=40)
    telegram_user_id: Optional[int] = Field(
        None,
        description=(
            "Numeric Telegram user id (message.from.id). Stable for "
            "the lifetime of the account; preferred over telegram_username."
        ),
    )
    telegram_username: Optional[str] = Field(
        None,
        max_length=64,
        description="Telegram @username without the leading @.",
    )
    interests_and_activities: Optional[str] = None
    notes: Optional[str] = None


class PersonCreate(PersonBase):
    family_id: int


class PersonUpdate(BaseModel):
    first_name: Optional[str] = Field(None, max_length=80)
    middle_name: Optional[str] = Field(None, max_length=80)
    last_name: Optional[str] = Field(None, max_length=80)
    preferred_name: Optional[str] = Field(None, max_length=80)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, max_length=32)
    primary_family_relationship: Optional[str] = Field(None, max_length=40)
    email_address: Optional[EmailStr] = None
    work_email: Optional[EmailStr] = None
    mobile_phone_number: Optional[str] = Field(None, max_length=40)
    home_phone_number: Optional[str] = Field(None, max_length=40)
    work_phone_number: Optional[str] = Field(None, max_length=40)
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = Field(None, max_length=64)
    interests_and_activities: Optional[str] = None
    notes: Optional[str] = None


class PersonRead(OrmModel):
    person_id: int
    family_id: int
    first_name: str
    middle_name: Optional[str]
    last_name: str
    preferred_name: Optional[str]
    date_of_birth: Optional[date]
    gender: Optional[str]
    primary_family_relationship: Optional[str]
    email_address: Optional[str]
    work_email: Optional[str]
    mobile_phone_number: Optional[str]
    home_phone_number: Optional[str]
    work_phone_number: Optional[str]
    telegram_user_id: Optional[int]
    telegram_username: Optional[str]
    profile_photo_path: Optional[str]
    interests_and_activities: Optional[str]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class PersonSummary(OrmModel):
    person_id: int
    family_id: int
    first_name: str
    last_name: str
    preferred_name: Optional[str]
    primary_family_relationship: Optional[str]
    date_of_birth: Optional[date]
    profile_photo_path: Optional[str]
