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

router = APIRouter(prefix="/person-photos", tags=["person_photos"])


@router.get("", response_model=List[schemas.PersonPhotoRead])
def list_person_photos(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.PersonPhoto]:
    stmt = select(models.PersonPhoto).order_by(models.PersonPhoto.created_at.desc())
    if person_id is not None:
        stmt = stmt.where(models.PersonPhoto.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=schemas.PersonPhotoRead, status_code=status.HTTP_201_CREATED)
def upload_person_photo(
    person_id: int = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    use_for_face_recognition: bool = Form(True),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.PersonPhoto:
    person = db.get(models.Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Person photos must be images.")

    rel_path, size, mime = storage.save_person_photo(
        person.family_id, person.person_id, file.file, file.filename or "photo.jpg"
    )
    photo = models.PersonPhoto(
        person_id=person.person_id,
        title=title,
        description=description,
        use_for_face_recognition=use_for_face_recognition,
        stored_file_path=rel_path,
        original_file_name=file.filename or "photo.jpg",
        mime_type=mime,
        file_size_bytes=size,
    )
    db.add(photo)
    db.flush()
    db.refresh(photo)
    return photo


@router.patch("/{person_photo_id}", response_model=schemas.PersonPhotoRead)
def update_person_photo(
    person_photo_id: int,
    payload: schemas.PersonPhotoUpdate,
    db: Session = Depends(get_db),
) -> models.PersonPhoto:
    photo = db.get(models.PersonPhoto, person_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(photo, field, value)
    db.flush()
    db.refresh(photo)
    return photo


@router.delete("/{person_photo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person_photo(person_photo_id: int, db: Session = Depends(get_db)) -> None:
    photo = db.get(models.PersonPhoto, person_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    storage.delete_if_exists(photo.stored_file_path)
    db.delete(photo)
