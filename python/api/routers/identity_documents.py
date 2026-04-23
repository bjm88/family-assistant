from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas, storage
from ..auth import require_admin
from ..db import get_db

# Admin-only — passports, driver's licenses, scanned ID images.
router = APIRouter(
    prefix="/identity-documents",
    tags=["identity_documents"],
    dependencies=[Depends(require_admin)],
)

Side = Literal["front", "back"]


def _image_path_attr(side: Side) -> str:
    return "front_image_path" if side == "front" else "back_image_path"


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
    # Clean up any image scans we stored for this document.
    storage.delete_if_exists(doc.front_image_path)
    storage.delete_if_exists(doc.back_image_path)
    db.delete(doc)


@router.post(
    "/{identity_document_id}/images/{side}",
    response_model=schemas.IdentityDocumentRead,
)
def upload_identity_document_image(
    identity_document_id: int,
    side: Side,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.IdentityDocument:
    """Attach a front- or back-side scan/photo to an identity document.

    Replaces any previous image stored for that side. The old file (if any)
    is deleted from disk after the new one is written.
    """
    doc = db.get(models.IdentityDocument, identity_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Identity document not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400, detail="Identity document scans must be images."
        )

    person = db.get(models.Person, doc.person_id)
    if person is None:
        # Should not happen given FK constraints, but guard anyway.
        raise HTTPException(status_code=404, detail="Person not found")

    rel_path, _, _ = storage.save_identity_document_image(
        person.family_id,
        person.person_id,
        doc.identity_document_id,
        side,
        file.file,
        file.filename or f"{side}.jpg",
    )

    attr = _image_path_attr(side)
    previous = getattr(doc, attr)
    setattr(doc, attr, rel_path)
    db.flush()
    if previous and previous != rel_path:
        storage.delete_if_exists(previous)
    db.refresh(doc)
    return doc


@router.delete(
    "/{identity_document_id}/images/{side}",
    response_model=schemas.IdentityDocumentRead,
)
def delete_identity_document_image(
    identity_document_id: int,
    side: Side,
    db: Session = Depends(get_db),
) -> models.IdentityDocument:
    doc = db.get(models.IdentityDocument, identity_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Identity document not found")
    attr = _image_path_attr(side)
    previous = getattr(doc, attr)
    setattr(doc, attr, None)
    db.flush()
    storage.delete_if_exists(previous)
    db.refresh(doc)
    return doc
