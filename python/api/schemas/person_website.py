from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class PersonWebsiteBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=1, max_length=500)


class PersonWebsiteCreate(PersonWebsiteBase):
    person_id: int


class PersonWebsiteUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    url: Optional[str] = Field(None, min_length=1, max_length=500)


class PersonWebsiteRead(OrmModel):
    person_website_id: int
    person_id: int
    name: str
    url: str
    created_at: datetime
    updated_at: datetime
