from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(prefix="/api/residences", tags=["residences"])


def _unset_other_primaries(
    db: Session, family_id: int, exclude_residence_id: Optional[int] = None
) -> None:
    """Flip every other residence in the family off-primary so only one stays."""
    stmt = (
        update(models.Residence)
        .where(models.Residence.family_id == family_id)
        .where(models.Residence.is_primary_residence.is_(True))
        .values(is_primary_residence=False)
    )
    if exclude_residence_id is not None:
        stmt = stmt.where(models.Residence.residence_id != exclude_residence_id)
    db.execute(stmt)


@router.get("", response_model=List[schemas.ResidenceRead])
def list_residences(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Residence]:
    stmt = select(models.Residence).order_by(
        models.Residence.is_primary_residence.desc(),
        models.Residence.label,
    )
    if family_id is not None:
        stmt = stmt.where(models.Residence.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.get("/{residence_id}", response_model=schemas.ResidenceRead)
def get_residence(residence_id: int, db: Session = Depends(get_db)) -> models.Residence:
    residence = db.get(models.Residence, residence_id)
    if residence is None:
        raise HTTPException(status_code=404, detail="Residence not found")
    return residence


@router.post(
    "", response_model=schemas.ResidenceRead, status_code=status.HTTP_201_CREATED
)
def create_residence(
    payload: schemas.ResidenceCreate, db: Session = Depends(get_db)
) -> models.Residence:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    residence = models.Residence(**payload.model_dump())
    db.add(residence)
    db.flush()

    if residence.is_primary_residence:
        _unset_other_primaries(
            db, residence.family_id, exclude_residence_id=residence.residence_id
        )

    db.refresh(residence)
    return residence


@router.patch("/{residence_id}", response_model=schemas.ResidenceRead)
def update_residence(
    residence_id: int,
    payload: schemas.ResidenceUpdate,
    db: Session = Depends(get_db),
) -> models.Residence:
    residence = db.get(models.Residence, residence_id)
    if residence is None:
        raise HTTPException(status_code=404, detail="Residence not found")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(residence, field, value)
    db.flush()

    if data.get("is_primary_residence") is True:
        _unset_other_primaries(
            db, residence.family_id, exclude_residence_id=residence.residence_id
        )

    db.refresh(residence)
    return residence


@router.delete("/{residence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_residence(residence_id: int, db: Session = Depends(get_db)) -> None:
    residence = db.get(models.Residence, residence_id)
    if residence is None:
        raise HTTPException(status_code=404, detail="Residence not found")
    db.delete(residence)
