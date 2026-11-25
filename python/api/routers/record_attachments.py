"""Photos and PDFs attached to an insurance policy or a vehicle.

Two routers with the same routes, one per kind of record:

    GET    /insurance-policies/{id}/attachments
    POST   /insurance-policies/{id}/attachments          multipart upload
    PATCH  /insurance-policies/{id}/attachments/{attachment_id}   kind, label
    GET    /insurance-policies/{id}/attachments/{attachment_id}/file
    DELETE /insurance-policies/{id}/attachments/{attachment_id}

    …and the same under /vehicles/{id}/attachments.

An insurance policy takes pictures of the insurance card and the policy
document; a vehicle takes its title and registration. A file must be a
photo or a PDF, judged by its contents rather than its name.

Admins only, like the rest of the insurance records: an insurance card
or a vehicle title shows numbers the database otherwise keeps encrypted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Type

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..auth import require_admin
from ..db import get_db


# What the first bytes of a file say it is -> (mime type, file extension).
# SVG is left out on purpose: it can carry scripts, and these files are
# shown in the browser.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def _sniff(head: bytes) -> Optional[Tuple[str, str]]:
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tif"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "image/heic", ".heic"
    return None


@dataclass(frozen=True)
class _Owner:
    """One kind of record that files can be attached to."""

    prefix: str  # "/vehicles/{owner_id}/attachments"
    tag: str
    owner_model: Type
    owner_name: str  # for "… not found"
    attachment_model: Type
    id_attr: str  # primary key of the attachment table
    owner_fk: str  # column pointing back at the owner
    kinds: Tuple[str, ...]
    save: Callable[..., Tuple[str, int]]


def _to_read(owner: _Owner, row) -> schemas.RecordAttachmentRead:
    return schemas.RecordAttachmentRead(
        attachment_id=getattr(row, owner.id_attr),
        owner_id=getattr(row, owner.owner_fk),
        attachment_kind=row.attachment_kind,
        label=row.label,
        original_file_name=row.original_file_name,
        mime_type=row.mime_type,
        file_size_bytes=row.file_size_bytes,
        is_image=row.is_image,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _build_router(owner: _Owner) -> APIRouter:
    router = APIRouter(
        prefix=owner.prefix,
        tags=[owner.tag],
        dependencies=[Depends(require_admin)],
    )

    def require_owner(db: Session, owner_id: int):
        row = db.get(owner.owner_model, owner_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{owner.owner_name} not found")
        return row

    def check_kind(kind: str) -> None:
        if kind not in owner.kinds:
            raise HTTPException(
                status_code=400,
                detail="Kind must be one of: " + ", ".join(owner.kinds) + ".",
            )

    def require_attachment(db: Session, owner_id: int, attachment_id: int):
        row = db.get(owner.attachment_model, attachment_id)
        if row is None or getattr(row, owner.owner_fk) != owner_id:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return row

    @router.get("", response_model=List[schemas.RecordAttachmentRead])
    def list_attachments(owner_id: int, db: Session = Depends(get_db)):
        require_owner(db, owner_id)
        model = owner.attachment_model
        rows = db.execute(
            select(model)
            .where(getattr(model, owner.owner_fk) == owner_id)
            .order_by(getattr(model, owner.id_attr))
        ).scalars()
        return [_to_read(owner, r) for r in rows]

    @router.post(
        "",
        response_model=schemas.RecordAttachmentRead,
        status_code=status.HTTP_201_CREATED,
    )
    def upload_attachment(
        owner_id: int,
        file: UploadFile = File(...),
        attachment_kind: str = Form(owner.kinds[0]),
        label: Optional[str] = Form(None),
        db: Session = Depends(get_db),
    ):
        record = require_owner(db, owner_id)
        check_kind(attachment_kind)
        label = (label or "").strip() or None
        if label and len(label) > 200:
            raise HTTPException(
                status_code=400, detail="Label must be 200 characters or fewer."
            )

        head = file.file.read(64)
        file.file.seek(0)
        if not head:
            raise HTTPException(status_code=400, detail="That file is empty.")
        sniffed = _sniff(head)
        if sniffed is None:
            raise HTTPException(
                status_code=400,
                detail="Only photos (JPEG, PNG, HEIC, WebP, GIF, TIFF) and PDFs can be attached.",
            )
        mime, extension = sniffed

        # Stored under an extension that matches the contents, whatever
        # the upload was called.
        rel_path, size = owner.save(
            record.family_id, owner_id, file.file, f"upload{extension}"
        )
        row = owner.attachment_model(
            **{owner.owner_fk: owner_id},
            attachment_kind=attachment_kind,
            label=label,
            stored_file_path=rel_path,
            original_file_name=(file.filename or f"upload{extension}")[:255],
            mime_type=mime,
            file_size_bytes=size,
        )
        db.add(row)
        try:
            db.flush()
        except Exception:
            storage.delete_if_exists(rel_path)
            raise
        db.refresh(row)
        return _to_read(owner, row)

    @router.patch("/{attachment_id}", response_model=schemas.RecordAttachmentRead)
    def update_attachment(
        owner_id: int,
        attachment_id: int,
        payload: schemas.RecordAttachmentUpdate,
        db: Session = Depends(get_db),
    ):
        row = require_attachment(db, owner_id, attachment_id)
        data = payload.model_dump(exclude_unset=True)
        if data.get("attachment_kind") is not None:
            check_kind(data["attachment_kind"])
            row.attachment_kind = data["attachment_kind"]
        if "label" in data:
            row.label = (data["label"] or "").strip() or None
        db.flush()
        db.refresh(row)
        return _to_read(owner, row)

    @router.get("/{attachment_id}/file")
    def get_attachment_file(
        owner_id: int, attachment_id: int, db: Session = Depends(get_db)
    ):
        """The file itself, shown in the browser rather than downloaded."""
        row = require_attachment(db, owner_id, attachment_id)
        path = storage.absolute_path(row.stored_file_path)
        if not path.is_file():
            raise HTTPException(
                status_code=410, detail="Attachment file is missing on disk."
            )
        return FileResponse(
            path,
            media_type=row.mime_type,
            filename=row.original_file_name,
            content_disposition_type="inline",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-cache",
            },
        )

    @router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_attachment(
        owner_id: int, attachment_id: int, db: Session = Depends(get_db)
    ) -> None:
        row = require_attachment(db, owner_id, attachment_id)
        stored = row.stored_file_path
        db.delete(row)
        db.flush()
        storage.delete_if_exists(stored)

    return router


insurance_router = _build_router(
    _Owner(
        prefix="/insurance-policies/{owner_id}/attachments",
        tag="insurance_policies",
        owner_model=models.InsurancePolicy,
        owner_name="Insurance policy",
        attachment_model=models.InsurancePolicyAttachment,
        id_attr="insurance_policy_attachment_id",
        owner_fk="insurance_policy_id",
        kinds=models.INSURANCE_POLICY_ATTACHMENT_KINDS,
        save=storage.save_insurance_policy_attachment,
    )
)

vehicle_router = _build_router(
    _Owner(
        prefix="/vehicles/{owner_id}/attachments",
        tag="vehicles",
        owner_model=models.Vehicle,
        owner_name="Vehicle",
        attachment_model=models.VehicleAttachment,
        id_attr="vehicle_attachment_id",
        owner_fk="vehicle_id",
        kinds=models.VEHICLE_ATTACHMENT_KINDS,
        save=storage.save_vehicle_attachment,
    )
)
