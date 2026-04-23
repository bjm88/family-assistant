from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..auth import require_admin
from ..db import get_db

# Admin-only — encrypted SSNs / account numbers / VINs. Members never
# see this surface, even on their own profile.
router = APIRouter(
    prefix="/sensitive-identifiers",
    tags=["sensitive_identifiers"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=List[schemas.SensitiveIdentifierRead])
def list_sensitive_identifiers(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.SensitiveIdentifier]:
    stmt = select(models.SensitiveIdentifier)
    if person_id is not None:
        stmt = stmt.where(models.SensitiveIdentifier.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=schemas.SensitiveIdentifierRead,
    status_code=status.HTTP_201_CREATED,
)
def create_sensitive_identifier(
    payload: schemas.SensitiveIdentifierCreate,
    db: Session = Depends(get_db),
) -> models.SensitiveIdentifier:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    row = models.SensitiveIdentifier(
        person_id=payload.person_id,
        identifier_type=payload.identifier_type,
        identifier_value_encrypted=crypto.encrypt_str(payload.identifier_value),
        identifier_last_four=crypto.last_four(payload.identifier_value),
        notes=payload.notes,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch("/{sensitive_identifier_id}", response_model=schemas.SensitiveIdentifierRead)
def update_sensitive_identifier(
    sensitive_identifier_id: int,
    payload: schemas.SensitiveIdentifierUpdate,
    db: Session = Depends(get_db),
) -> models.SensitiveIdentifier:
    row = db.get(models.SensitiveIdentifier, sensitive_identifier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sensitive identifier not found")
    data = payload.model_dump(exclude_unset=True)
    value = data.pop("identifier_value", None)
    for field, v in data.items():
        setattr(row, field, v)
    if value is not None:
        row.identifier_value_encrypted = crypto.encrypt_str(value)
        row.identifier_last_four = crypto.last_four(value)
    db.flush()
    db.refresh(row)
    return row


@router.delete("/{sensitive_identifier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sensitive_identifier(
    sensitive_identifier_id: int, db: Session = Depends(get_db)
) -> None:
    row = db.get(models.SensitiveIdentifier, sensitive_identifier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sensitive identifier not found")
    db.delete(row)
