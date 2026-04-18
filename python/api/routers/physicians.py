"""CRUD for ``physicians`` (per-person care relationships)."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db


router = APIRouter(prefix="/physicians", tags=["physicians"])


@router.get("", response_model=List[schemas.PhysicianRead])
def list_physicians(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Physician]:
    stmt = select(models.Physician)
    if person_id is not None:
        stmt = stmt.where(models.Physician.person_id == person_id)
    rows = list(db.execute(stmt).scalars())
    rows.sort(key=lambda p: (p.physician_name or "").lower())
    return rows


@router.post(
    "",
    response_model=schemas.PhysicianRead,
    status_code=status.HTTP_201_CREATED,
)
def create_physician(
    payload: schemas.PhysicianCreate,
    db: Session = Depends(get_db),
) -> models.Physician:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.Physician(**payload.model_dump())
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch("/{physician_id}", response_model=schemas.PhysicianRead)
def update_physician(
    physician_id: int,
    payload: schemas.PhysicianUpdate,
    db: Session = Depends(get_db),
) -> models.Physician:
    row = db.get(models.Physician, physician_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Physician not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.flush()
    db.refresh(row)
    return row


@router.delete(
    "/{physician_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_physician(
    physician_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(models.Physician, physician_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Physician not found")
    db.delete(row)
