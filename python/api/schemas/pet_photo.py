from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from ._base import OrmModel


class PetPhotoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class PetPhotoRead(OrmModel):
    pet_photo_id: int
    pet_id: int
    title: str
    description: Optional[str]
    stored_file_path: str
    original_file_name: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    created_at: datetime
    updated_at: datetime
