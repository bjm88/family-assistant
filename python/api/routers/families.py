from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(prefix="/api/families", tags=["families"])


@router.get("", response_model=List[schemas.FamilySummary])
def list_families(db: Session = Depends(get_db)) -> List[schemas.FamilySummary]:
    rows = db.execute(
        select(
            models.Family,
            func.count(models.Person.person_id.distinct()).label("people_count"),
        )
        .outerjoin(models.Person, models.Person.family_id == models.Family.family_id)
        .group_by(models.Family.family_id)
        .order_by(models.Family.family_name)
    ).all()

    summaries: List[schemas.FamilySummary] = []
    for family, people_count in rows:
        summaries.append(
            schemas.FamilySummary(
                family_id=family.family_id,
                family_name=family.family_name,
                people_count=people_count or 0,
                vehicles_count=len(family.vehicles),
                insurance_policies_count=len(family.insurance_policies),
                financial_accounts_count=len(family.financial_accounts),
                documents_count=len(family.documents),
            )
        )
    return summaries


@router.post("", response_model=schemas.FamilyRead, status_code=status.HTTP_201_CREATED)
def create_family(payload: schemas.FamilyCreate, db: Session = Depends(get_db)) -> models.Family:
    family = models.Family(**payload.model_dump())
    db.add(family)
    db.flush()
    db.refresh(family)
    return family


@router.get("/{family_id}", response_model=schemas.FamilyRead)
def get_family(family_id: int, db: Session = Depends(get_db)) -> models.Family:
    family = db.get(models.Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")
    return family


@router.patch("/{family_id}", response_model=schemas.FamilyRead)
def update_family(
    family_id: int,
    payload: schemas.FamilyUpdate,
    db: Session = Depends(get_db),
) -> models.Family:
    family = db.get(models.Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(family, field, value)
    db.flush()
    db.refresh(family)
    return family


@router.delete("/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_family(family_id: int, db: Session = Depends(get_db)) -> None:
    family = db.get(models.Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")
    db.delete(family)
