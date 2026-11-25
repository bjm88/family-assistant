from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RecordAttachmentUpdate(BaseModel):
    """Re-file an attachment: change what kind it is, or its label."""

    attachment_kind: Optional[str] = None
    # An empty string clears the label.
    label: Optional[str] = Field(None, max_length=200)


class RecordAttachmentRead(BaseModel):
    """A photo or PDF attached to an insurance policy or a vehicle."""

    attachment_id: int
    # The insurance policy or vehicle the file belongs to.
    owner_id: int
    attachment_kind: str
    label: Optional[str]
    original_file_name: str
    mime_type: str
    file_size_bytes: Optional[int]
    # False for a PDF.
    is_image: bool
    created_at: datetime
    updated_at: datetime
