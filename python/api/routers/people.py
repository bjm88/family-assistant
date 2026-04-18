from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..db import get_db

router = APIRouter(prefix="/people", tags=["people"])


def _get_person_or_404(db: Session, person_id: int) -> models.Person:
    person = db.get(models.Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


@router.get("", response_model=List[schemas.PersonSummary])
def list_people(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Person]:
    stmt = select(models.Person).order_by(models.Person.last_name, models.Person.first_name)
    if family_id is not None:
        stmt = stmt.where(models.Person.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=schemas.PersonRead, status_code=status.HTTP_201_CREATED)
def create_person(payload: schemas.PersonCreate, db: Session = Depends(get_db)) -> models.Person:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    person = models.Person(**payload.model_dump())
    db.add(person)
    db.flush()
    db.refresh(person)
    return person


@router.get("/{person_id}", response_model=schemas.PersonRead)
def get_person(person_id: int, db: Session = Depends(get_db)) -> models.Person:
    return _get_person_or_404(db, person_id)


@router.patch("/{person_id}", response_model=schemas.PersonRead)
def update_person(
    person_id: int,
    payload: schemas.PersonUpdate,
    db: Session = Depends(get_db),
) -> models.Person:
    person = _get_person_or_404(db, person_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(person, field, value)
    db.flush()
    db.refresh(person)
    return person


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person(person_id: int, db: Session = Depends(get_db)) -> None:
    person = _get_person_or_404(db, person_id)
    storage.delete_if_exists(person.profile_photo_path)
    db.delete(person)


@router.post("/{person_id}/profile-photo", response_model=schemas.PersonRead)
def upload_profile_photo(
    person_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.Person:
    person = _get_person_or_404(db, person_id)
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Profile photo must be an image.")
    storage.delete_if_exists(person.profile_photo_path)
    rel_path, _ = storage.save_profile_photo(
        person.family_id, person.person_id, file.file, file.filename or "photo.jpg"
    )
    person.profile_photo_path = rel_path
    db.flush()
    db.refresh(person)
    return person
