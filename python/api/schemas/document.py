from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from ._base import OrmModel


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    document_category: Optional[str] = None
    person_id: Optional[int] = None
    notes: Optional[str] = None


class DocumentRead(OrmModel):
    document_id: int
    family_id: int
    person_id: Optional[int]
    title: str
    document_category: Optional[str]
    original_file_name: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
