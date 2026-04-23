"""CRUD for ``medical_conditions``.

Open conditions (``end_date IS NULL``) come first so the admin UI can
glance and see what's currently active without scrolling. Within each
group rows are ordered by ``start_date DESC`` (most recent first), with
``NULL`` start dates falling to the bottom.
"""

from __future__ import annotations

from datetime import date as _date_cls
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..db import get_db


router = APIRouter(
    prefix="/medical-conditions",
    tags=["medical-conditions"],
    dependencies=[Depends(require_admin)],
)


# Sentinel used so rows with no start_date sort *after* dated rows
# regardless of whether we're sorting ascending or descending.
_FAR_PAST = _date_cls(1, 1, 1)


@router.get("", response_model=List[schemas.MedicalConditionRead])
def list_medical_conditions(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.MedicalCondition]:
    stmt = select(models.MedicalCondition)
    if person_id is not None:
        stmt = stmt.where(models.MedicalCondition.person_id == person_id)
    rows = list(db.execute(stmt).scalars())
    rows.sort(
        key=lambda c: (
            c.end_date is not None,  # open (False) sorts before closed (True)
            -(c.start_date or _FAR_PAST).toordinal(),
            c.medical_condition_id,
        )
    )
    return rows


@router.post(
    "",
    response_model=schemas.MedicalConditionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_medical_condition(
    payload: schemas.MedicalConditionCreate,
    db: Session = Depends(get_db),
) -> models.MedicalCondition:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.MedicalCondition(**payload.model_dump())
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch(
    "/{medical_condition_id}",
    response_model=schemas.MedicalConditionRead,
)
def update_medical_condition(
    medical_condition_id: int,
    payload: schemas.MedicalConditionUpdate,
    db: Session = Depends(get_db),
) -> models.MedicalCondition:
    row = db.get(models.MedicalCondition, medical_condition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Medical condition not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.flush()
    db.refresh(row)
    return row


@router.delete(
    "/{medical_condition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_medical_condition(
    medical_condition_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(models.MedicalCondition, medical_condition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Medical condition not found")
    db.delete(row)
