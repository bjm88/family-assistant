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


# ---- Face landmark geometry (derived, not stored) --------------------------
# All coordinates are expressed as percentages of the avatar image (0..1)
# so the frontend can overlay them on a responsive <img> without needing
# to know the raw pixel size. These are produced lazily by InsightFace in
# `routers/assistants.py` and cached in memory per image path.


class _BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class _MouthBox(BaseModel):
    cx: float
    cy: float
    w: float
    h: float


class _Eyes(BaseModel):
    lx: float
    ly: float
    rx: float
    ry: float


class AvatarLandmarks(BaseModel):
    """Face geometry detected on the assistant's avatar image.

    Returned on ``AssistantRead`` whenever InsightFace successfully finds
    a face; ``None`` when the avatar image is missing, the detector has
    not been initialised yet, or no face is visible (e.g. stylised mask).
    The frontend uses ``mouth`` to place the lip-sync SVG overlay.
    """

    bbox: _BBox
    mouth: _MouthBox
    eyes: _Eyes


class AssistantRead(OrmModel):
    assistant_id: int
    family_id: int
    assistant_name: str
    gender: Optional[str]
    visual_description: Optional[str]
    personality_description: Optional[str]
    profile_image_path: Optional[str]
    avatar_generation_note: Optional[str]
    avatar_landmarks: Optional[AvatarLandmarks] = None
    created_at: datetime
    updated_at: datetime
