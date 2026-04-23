from __future__ import annotations

from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..auth import require_admin
from ..db import get_db

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=List[schemas.DocumentRead])
def list_documents(
    family_id: Optional[int] = Query(None),
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Document]:
    stmt = select(models.Document).order_by(models.Document.created_at.desc())
    if family_id is not None:
        stmt = stmt.where(models.Document.family_id == family_id)
    if person_id is not None:
        stmt = stmt.where(models.Document.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=schemas.DocumentRead, status_code=status.HTTP_201_CREATED)
def upload_document(
    family_id: int = Form(...),
    title: str = Form(...),
    document_category: Optional[str] = Form(None),
    person_id: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.Document:
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    if person_id is not None and db.get(models.Person, person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    rel_path, size, mime = storage.save_document(
        family_id, person_id, file.file, file.filename or "upload.bin"
    )
    doc = models.Document(
        family_id=family_id,
        person_id=person_id,
        title=title,
        document_category=document_category,
        stored_file_path=rel_path,
        original_file_name=file.filename or "upload.bin",
        mime_type=mime,
        file_size_bytes=size,
        notes=notes,
    )
    db.add(doc)
    db.flush()
    db.refresh(doc)
    return doc


@router.patch("/{document_id}", response_model=schemas.DocumentRead)
def update_document(
    document_id: int,
    payload: schemas.DocumentUpdate,
    db: Session = Depends(get_db),
) -> models.Document:
    doc = db.get(models.Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    db.flush()
    db.refresh(doc)
    return doc


@router.get("/{document_id}/download")
def download_document(document_id: int, db: Session = Depends(get_db)):
    doc = db.get(models.Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    path = storage.absolute_path(doc.stored_file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="Document file is missing on disk.")
    return FileResponse(
        path,
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.original_file_name,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: int, db: Session = Depends(get_db)) -> None:
    doc = db.get(models.Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    storage.delete_if_exists(doc.stored_file_path)
    db.delete(doc)
