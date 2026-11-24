"""CRUD for ``person_websites`` — named URLs belonging to a person.

Mirrors the shape of ``jobs``: a person-scoped list endpoint, plus
create / update / delete. Rows for a single person sort by name
(then id) so the admin UI shows a stable order without callers
needing to specify one.
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
    prefix="/person-websites",
    tags=["person-websites"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=List[schemas.PersonWebsiteRead])
def list_person_websites(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.PersonWebsite]:
    stmt = select(models.PersonWebsite)
    if person_id is not None:
        stmt = stmt.where(models.PersonWebsite.person_id == person_id)
    rows = list(db.execute(stmt).scalars())
    rows.sort(
        key=lambda w: (
            (w.name or "").lower(),
            w.person_website_id,
        )
    )
    return rows


@router.post(
    "",
    response_model=schemas.PersonWebsiteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_person_website(
    payload: schemas.PersonWebsiteCreate,
    db: Session = Depends(get_db),
) -> models.PersonWebsite:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.PersonWebsite(**payload.model_dump())
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch(
    "/{person_website_id}",
    response_model=schemas.PersonWebsiteRead,
)
def update_person_website(
    person_website_id: int,
    payload: schemas.PersonWebsiteUpdate,
    db: Session = Depends(get_db),
) -> models.PersonWebsite:
    row = db.get(models.PersonWebsite, person_website_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Website not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.flush()
    db.refresh(row)
    return row


@router.delete(
    "/{person_website_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_person_website(
    person_website_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(models.PersonWebsite, person_website_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Website not found")
    db.delete(row)
