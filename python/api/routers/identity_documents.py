from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..db import get_db

router = APIRouter(prefix="/api/identity-documents", tags=["identity_documents"])


@router.get("", response_model=List[schemas.IdentityDocumentRead])
def list_identity_documents(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.IdentityDocument]:
    stmt = select(models.IdentityDocument).order_by(
        models.IdentityDocument.expiration_date.nulls_last()
    )
    if person_id is not None:
        stmt = stmt.where(models.IdentityDocument.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=schemas.IdentityDocumentRead, status_code=status.HTTP_201_CREATED)
def create_identity_document(
    payload: schemas.IdentityDocumentCreate,
    db: Session = Depends(get_db),
) -> models.IdentityDocument:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    data = payload.model_dump(exclude={"document_number"})
    doc = models.IdentityDocument(**data)
    if payload.document_number:
        doc.document_number_encrypted = crypto.encrypt_str(payload.document_number)
        doc.document_number_last_four = crypto.last_four(payload.document_number)
    db.add(doc)
    db.flush()
    db.refresh(doc)
    return doc


@router.patch("/{identity_document_id}", response_model=schemas.IdentityDocumentRead)
def update_identity_document(
    identity_document_id: int,
    payload: schemas.IdentityDocumentUpdate,
    db: Session = Depends(get_db),
) -> models.IdentityDocument:
    doc = db.get(models.IdentityDocument, identity_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Identity document not found")
    updates = payload.model_dump(exclude_unset=True)
    document_number = updates.pop("document_number", None)
    for field, value in updates.items():
        setattr(doc, field, value)
    if document_number is not None:
        if document_number == "":
            doc.document_number_encrypted = None
            doc.document_number_last_four = None
        else:
            doc.document_number_encrypted = crypto.encrypt_str(document_number)
            doc.document_number_last_four = crypto.last_four(document_number)
    db.flush()
    db.refresh(doc)
    return doc


@router.delete("/{identity_document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_identity_document(identity_document_id: int, db: Session = Depends(get_db)) -> None:
    doc = db.get(models.IdentityDocument, identity_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Identity document not found")
    db.delete(doc)
