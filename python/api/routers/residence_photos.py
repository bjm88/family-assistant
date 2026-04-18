from __future__ import annotations

from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..db import get_db

router = APIRouter(prefix="/residence-photos", tags=["residence_photos"])


@router.get("", response_model=List[schemas.ResidencePhotoRead])
def list_residence_photos(
    residence_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.ResidencePhoto]:
    stmt = select(models.ResidencePhoto).order_by(
        models.ResidencePhoto.created_at.desc()
    )
    if residence_id is not None:
        stmt = stmt.where(models.ResidencePhoto.residence_id == residence_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=schemas.ResidencePhotoRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_residence_photo(
    residence_id: int = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.ResidencePhoto:
    residence = db.get(models.Residence, residence_id)
    if residence is None:
        raise HTTPException(status_code=404, detail="Residence not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400, detail="Residence photos must be images."
        )

    rel_path, size, mime = storage.save_residence_photo(
        residence.family_id,
        residence.residence_id,
        file.file,
        file.filename or "photo.jpg",
    )
    photo = models.ResidencePhoto(
        residence_id=residence.residence_id,
        title=title,
        description=description,
        stored_file_path=rel_path,
        original_file_name=file.filename or "photo.jpg",
        mime_type=mime,
        file_size_bytes=size,
    )
    db.add(photo)
    db.flush()
    db.refresh(photo)
    return photo


@router.patch("/{residence_photo_id}", response_model=schemas.ResidencePhotoRead)
def update_residence_photo(
    residence_photo_id: int,
    payload: schemas.ResidencePhotoUpdate,
    db: Session = Depends(get_db),
) -> models.ResidencePhoto:
    photo = db.get(models.ResidencePhoto, residence_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(photo, field, value)
    db.flush()
    db.refresh(photo)
    return photo


@router.delete("/{residence_photo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_residence_photo(
    residence_photo_id: int, db: Session = Depends(get_db)
) -> None:
    photo = db.get(models.ResidencePhoto, residence_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    storage.delete_if_exists(photo.stored_file_path)
    db.delete(photo)
