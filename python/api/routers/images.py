"""Rotate a stored image and save the result.

One endpoint for every picture the admin UI shows: the scans on an
identity document, an image saved as a document, a person's profile
picture and extra photos, pet and residence photos, and a vehicle's
picture.

``POST /api/admin/images/rotate`` takes what the picture belongs to
and how far to turn it clockwise (90, 180 or 270 degrees). The rotated
picture is written as a new file, the record is pointed at it, and the
old file is deleted — see :mod:`api.services.image_rotation`.

A person photo used for face recognition has its face data recomputed
from the rotated picture.
"""

from __future__ import annotations

from typing import Literal, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai.enrollment import reenroll_photo
from ..auth import require_admin
from ..db import get_db
from ..services import image_rotation

# Admin-only, like uploading or replacing any of these images.
router = APIRouter(
    prefix="/images",
    tags=["images"],
    dependencies=[Depends(require_admin)],
)

ImageKind = Literal[
    "identity_document",
    "document",
    "person_profile",
    "person_photo",
    "pet_photo",
    "residence_photo",
    "vehicle_photo",
]


class RotateImageRequest(BaseModel):
    kind: ImageKind
    # The id of the record the image belongs to: the identity document,
    # document, person, person photo, pet photo, residence photo or
    # vehicle.
    record_id: int
    # Identity documents only: which scan.
    side: Optional[Literal["front", "back"]] = None
    # Clockwise.
    degrees: Literal[90, 180, 270]


class RotateImageResponse(BaseModel):
    kind: ImageKind
    record_id: int
    side: Optional[str] = None
    # The rotated image, relative to the storage root (``/api/media/<path>``).
    path: str
    size_bytes: int
    width: int
    height: int


def _find(
    db: Session, req: RotateImageRequest
) -> Tuple[object, str, Optional[str]]:
    """The record, the column holding the image path, and the size column."""
    if req.kind == "identity_document":
        row = db.get(models.IdentityDocument, req.record_id)
        if req.side is None:
            raise HTTPException(
                status_code=400, detail="Say which side to rotate: front or back."
            )
        path_attr = "front_image_path" if req.side == "front" else "back_image_path"
        return _required(row, "Identity document"), path_attr, None
    if req.kind == "document":
        row = _required(db.get(models.Document, req.record_id), "Document")
        if not (row.mime_type or "").startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail="Only documents that are images can be rotated.",
            )
        return row, "stored_file_path", "file_size_bytes"
    if req.kind == "person_profile":
        return _required(db.get(models.Person, req.record_id), "Person"), "profile_photo_path", None
    if req.kind == "person_photo":
        row = db.get(models.PersonPhoto, req.record_id)
        return _required(row, "Photo"), "stored_file_path", "file_size_bytes"
    if req.kind == "pet_photo":
        row = db.get(models.PetPhoto, req.record_id)
        return _required(row, "Photo"), "stored_file_path", "file_size_bytes"
    if req.kind == "residence_photo":
        row = db.get(models.ResidencePhoto, req.record_id)
        return _required(row, "Photo"), "stored_file_path", "file_size_bytes"
    row = db.get(models.Vehicle, req.record_id)
    return _required(row, "Vehicle"), "profile_image_path", None


def _required(row: object, what: str) -> object:
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


@router.post("/rotate", response_model=RotateImageResponse)
def rotate_image(
    req: RotateImageRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RotateImageResponse:
    row, path_attr, size_attr = _find(db, req)
    previous = getattr(row, path_attr)
    if not previous:
        raise HTTPException(status_code=404, detail="There is no image to rotate.")

    try:
        rotated = image_rotation.rotate_stored_image(previous, req.degrees)
    except image_rotation.ImageRotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    setattr(row, path_attr, rotated.relative_path)
    if size_attr:
        setattr(row, size_attr, rotated.size_bytes)
    try:
        # Commit before touching the old file, so the record never
        # points at a file that has been deleted.
        db.commit()
    except Exception:
        storage.delete_if_exists(rotated.relative_path)
        raise
    storage.delete_if_exists(previous)

    if req.kind == "person_photo":
        # The saved face data describes the picture as it was.
        background.add_task(reenroll_photo, req.record_id)

    return RotateImageResponse(
        kind=req.kind,
        record_id=req.record_id,
        side=req.side if req.kind == "identity_document" else None,
        path=rotated.relative_path,
        size_bytes=rotated.size_bytes,
        width=rotated.width,
        height=rotated.height,
    )
