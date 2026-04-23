"""CRUD for ``jobs`` — a person's employment / role history.

Mirrors the shape of ``medical_conditions``: a person-scoped list
endpoint, plus create / update / delete. Rows for a single person
sort by company name (then job_id) so the admin UI shows a stable
order without callers needing to specify one.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..db import get_db


router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=List[schemas.JobRead])
def list_jobs(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Job]:
    stmt = select(models.Job)
    if person_id is not None:
        stmt = stmt.where(models.Job.person_id == person_id)
    rows = list(db.execute(stmt).scalars())
    rows.sort(
        key=lambda j: (
            (j.company_name or "").lower(),
            j.job_id,
        )
    )
    return rows


@router.post(
    "",
    response_model=schemas.JobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    payload: schemas.JobCreate,
    db: Session = Depends(get_db),
) -> models.Job:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.Job(**payload.model_dump())
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch("/{job_id}", response_model=schemas.JobRead)
def update_job(
    job_id: int,
    payload: schemas.JobUpdate,
    db: Session = Depends(get_db),
) -> models.Job:
    row = db.get(models.Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.flush()
    db.refresh(row)
    return row


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(models.Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.delete(row)
