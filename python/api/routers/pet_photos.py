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

router = APIRouter(prefix="/pet-photos", tags=["pet_photos"])


@router.get("", response_model=List[schemas.PetPhotoRead])
def list_pet_photos(
    pet_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.PetPhoto]:
    stmt = select(models.PetPhoto).order_by(models.PetPhoto.created_at.desc())
    if pet_id is not None:
        stmt = stmt.where(models.PetPhoto.pet_id == pet_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "", response_model=schemas.PetPhotoRead, status_code=status.HTTP_201_CREATED
)
def upload_pet_photo(
    pet_id: int = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.PetPhoto:
    pet = db.get(models.Pet, pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="Pet not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Pet photos must be images.")

    rel_path, size, mime = storage.save_pet_photo(
        pet.family_id, pet.pet_id, file.file, file.filename or "photo.jpg"
    )
    photo = models.PetPhoto(
        pet_id=pet.pet_id,
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


@router.patch("/{pet_photo_id}", response_model=schemas.PetPhotoRead)
def update_pet_photo(
    pet_photo_id: int,
    payload: schemas.PetPhotoUpdate,
    db: Session = Depends(get_db),
) -> models.PetPhoto:
    photo = db.get(models.PetPhoto, pet_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(photo, field, value)
    db.flush()
    db.refresh(photo)
    return photo


@router.delete("/{pet_photo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pet_photo(pet_photo_id: int, db: Session = Depends(get_db)) -> None:
    photo = db.get(models.PetPhoto, pet_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    storage.delete_if_exists(photo.stored_file_path)
    db.delete(photo)
