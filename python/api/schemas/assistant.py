from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


Gender = Literal["male", "female"]


class AssistantBase(BaseModel):
    assistant_name: str = Field("Avi", max_length=80)
    gender: Optional[Gender] = None
    visual_description: Optional[str] = None
    personality_description: Optional[str] = None


class AssistantCreate(AssistantBase):
    family_id: int


class AssistantUpdate(BaseModel):
    assistant_name: Optional[str] = Field(None, max_length=80)
    gender: Optional[Gender] = None
    visual_description: Optional[str] = None
    personality_description: Optional[str] = None


class AssistantRead(OrmModel):
    assistant_id: int
    family_id: int
    assistant_name: str
    gender: Optional[str]
    visual_description: Optional[str]
    personality_description: Optional[str]
    profile_image_path: Optional[str]
    avatar_generation_note: Optional[str]
    created_at: datetime
    updated_at: datetime
