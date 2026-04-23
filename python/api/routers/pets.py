from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin, require_family_member_from_request
from ..db import get_db

router = APIRouter(prefix="/pets", tags=["pets"])


@router.get(
    "",
    response_model=List[schemas.PetRead],
    dependencies=[Depends(require_family_member_from_request)],
)
def list_pets(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Pet]:
    stmt = select(models.Pet).order_by(models.Pet.pet_name)
    if family_id is not None:
        stmt = stmt.where(models.Pet.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=schemas.PetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_pet(payload: schemas.PetCreate, db: Session = Depends(get_db)) -> models.Pet:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    pet = models.Pet(**payload.model_dump())
    db.add(pet)
    db.flush()
    db.refresh(pet)
    return pet


@router.patch(
    "/{pet_id}",
    response_model=schemas.PetRead,
    dependencies=[Depends(require_admin)],
)
def update_pet(
    pet_id: int,
    payload: schemas.PetUpdate,
    db: Session = Depends(get_db),
) -> models.Pet:
    pet = db.get(models.Pet, pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="Pet not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(pet, field, value)
    db.flush()
    db.refresh(pet)
    return pet


@router.delete(
    "/{pet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_pet(pet_id: int, db: Session = Depends(get_db)) -> None:
    pet = db.get(models.Pet, pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="Pet not found")
    db.delete(pet)
