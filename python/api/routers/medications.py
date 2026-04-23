"""CRUD for ``medications``.

Active meds (``end_date IS NULL``) come first so the "what is X
currently taking?" question gets a fast scan. The
:class:`MedicationCreate` schema enforces the
``ck_medications_at_least_one_identifier`` invariant at the API layer
to surface a friendly 422; the database keeps the same rule as a
defence in depth.
"""

from __future__ import annotations

from datetime import date as _date_cls
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..db import get_db


router = APIRouter(
    prefix="/medications",
    tags=["medications"],
    dependencies=[Depends(require_admin)],
)

_FAR_PAST = _date_cls(1, 1, 1)


@router.get("", response_model=List[schemas.MedicationRead])
def list_medications(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Medication]:
    stmt = select(models.Medication)
    if person_id is not None:
        stmt = stmt.where(models.Medication.person_id == person_id)
    rows = list(db.execute(stmt).scalars())
    rows.sort(
        key=lambda m: (
            m.end_date is not None,
            -(m.start_date or _FAR_PAST).toordinal(),
            m.medication_id,
        )
    )
    return rows


@router.post(
    "",
    response_model=schemas.MedicationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_medication(
    payload: schemas.MedicationCreate,
    db: Session = Depends(get_db),
) -> models.Medication:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.Medication(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Medication needs at least one of NDC number, generic "
                "name, or brand name."
            ),
        ) from exc
    db.refresh(row)
    return row


@router.patch("/{medication_id}", response_model=schemas.MedicationRead)
def update_medication(
    medication_id: int,
    payload: schemas.MedicationUpdate,
    db: Session = Depends(get_db),
) -> models.Medication:
    row = db.get(models.Medication, medication_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Medication not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Medication needs at least one of NDC number, generic "
                "name, or brand name."
            ),
        ) from exc
    db.refresh(row)
    return row


@router.delete(
    "/{medication_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_medication(
    medication_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(models.Medication, medication_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Medication not found")
    db.delete(row)
