from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from ._base import OrmModel


class PersonPhotoUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    use_for_face_recognition: Optional[bool] = None


class PersonPhotoRead(OrmModel):
    person_photo_id: int
    person_id: int
    title: str
    description: Optional[str]
    use_for_face_recognition: bool
    stored_file_path: str
    original_file_name: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    created_at: datetime
    updated_at: datetime
