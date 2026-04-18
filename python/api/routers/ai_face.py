"""Face enrollment + live recognition endpoints.

Enrollment walks every person_photo in a family that has
``use_for_face_recognition=True`` and extracts an InsightFace embedding
for any photo that doesn't already have one cached.

Recognition accepts a single webcam frame (multipart upload), extracts an
embedding, and returns the best matching person in the family above the
configured cosine-similarity threshold.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, storage
from ..ai import face as face_service
from ..config import get_settings
from ..db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/face", tags=["ai_face"])


# ---------- Schemas -------------------------------------------------------


class EnrollResult(BaseModel):
    person_photo_id: int
    person_id: int
    status: str  # "embedded" | "no_face" | "unchanged" | "error"
    detail: Optional[str] = None


class EnrollResponse(BaseModel):
    family_id: int
    total_candidates: int
    enrolled: int
    skipped_unchanged: int
    skipped_no_face: int
    errors: int
    results: List[EnrollResult]


class RecognizeResponse(BaseModel):
    matched: bool
    person_id: Optional[int] = None
    person_name: Optional[str] = None
    similarity: Optional[float] = None
    threshold: float
    reason: Optional[str] = None


class FaceStatus(BaseModel):
    providers: List[str]
    mac_studio_optimized: bool
    threshold: float
    enrolled_embeddings: int


# ---------- Helpers -------------------------------------------------------


def _display_name(p: models.Person) -> str:
    return p.preferred_name or p.first_name or f"Person {p.person_id}"


def _load_gallery(db: Session, family_id: int) -> List[face_service.EnrolledFace]:
    rows = db.execute(
        select(models.FaceEmbedding).where(
            models.FaceEmbedding.family_id == family_id
        )
    ).scalars().all()
    gallery: List[face_service.EnrolledFace] = []
    for r in rows:
        try:
            emb = face_service.decode_bytes(r.embedding_bytes)
        except Exception:
            continue
        gallery.append(
            face_service.EnrolledFace(person_id=r.person_id, embedding=emb)
        )
    return gallery


# ---------- Status --------------------------------------------------------


@router.get("/status", response_model=FaceStatus)
def status(
    family_id: int,
    db: Session = Depends(get_db),
) -> FaceStatus:
    s = get_settings()
    enrolled = db.execute(
        select(models.FaceEmbedding).where(
            models.FaceEmbedding.family_id == family_id
        )
    ).scalars().all()
    return FaceStatus(
        providers=face_service.providers_in_use(),
        mac_studio_optimized=s.AI_MAC_STUDIO_OPTIMIZED,
        threshold=s.AI_FACE_MATCH_THRESHOLD,
        enrolled_embeddings=len(enrolled),
    )


# ---------- Enroll --------------------------------------------------------


@router.post("/enroll", response_model=EnrollResponse)
def enroll_family(
    family_id: int,
    db: Session = Depends(get_db),
) -> EnrollResponse:
    """Extract embeddings for every recognition-flagged photo in this family."""
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    # Pull every candidate photo joined to its person so we can denormalize
    # person_id / family_id onto the embedding row.
    candidates = db.execute(
        select(models.PersonPhoto, models.Person)
        .join(models.Person, models.Person.person_id == models.PersonPhoto.person_id)
        .where(models.Person.family_id == family_id)
        .where(models.PersonPhoto.use_for_face_recognition.is_(True))
    ).all()

    enrolled = 0
    skipped_unchanged = 0
    skipped_no_face = 0
    errors = 0
    results: List[EnrollResult] = []

    for photo, person in candidates:
        existing = db.execute(
            select(models.FaceEmbedding).where(
                models.FaceEmbedding.person_photo_id == photo.person_photo_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped_unchanged += 1
            results.append(
                EnrollResult(
                    person_photo_id=photo.person_photo_id,
                    person_id=person.person_id,
                    status="unchanged",
                )
            )
            continue

        try:
            abs_path = storage.absolute_path(photo.stored_file_path)
            with open(abs_path, "rb") as fh:
                image_bytes = fh.read()
            extracted = face_service.extract_embedding(image_bytes)
        except Exception as e:  # noqa: BLE001
            errors += 1
            logger.exception("Face embedding failed for photo %s", photo.person_photo_id)
            results.append(
                EnrollResult(
                    person_photo_id=photo.person_photo_id,
                    person_id=person.person_id,
                    status="error",
                    detail=str(e)[:200],
                )
            )
            continue

        if extracted is None:
            skipped_no_face += 1
            results.append(
                EnrollResult(
                    person_photo_id=photo.person_photo_id,
                    person_id=person.person_id,
                    status="no_face",
                )
            )
            continue

        emb, bbox = extracted
        row = models.FaceEmbedding(
            person_photo_id=photo.person_photo_id,
            person_id=person.person_id,
            family_id=family_id,
            model_name="buffalo_l",
            embedding_dim=emb.shape[0],
            embedding_bytes=face_service.encode_bytes(emb),
            bounding_box_json=face_service.bbox_to_json(bbox),
        )
        db.add(row)
        enrolled += 1
        results.append(
            EnrollResult(
                person_photo_id=photo.person_photo_id,
                person_id=person.person_id,
                status="embedded",
            )
        )

    db.flush()

    return EnrollResponse(
        family_id=family_id,
        total_candidates=len(candidates),
        enrolled=enrolled,
        skipped_unchanged=skipped_unchanged,
        skipped_no_face=skipped_no_face,
        errors=errors,
        results=results,
    )


@router.delete("/enroll")
def clear_enrollments(
    family_id: int,
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(models.FaceEmbedding).where(
            models.FaceEmbedding.family_id == family_id
        )
    ).scalars().all()
    for r in rows:
        db.delete(r)
    return {"family_id": family_id, "deleted": len(rows)}


# ---------- Recognize -----------------------------------------------------


@router.post("/recognize", response_model=RecognizeResponse)
def recognize(
    family_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RecognizeResponse:
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    threshold = get_settings().AI_FACE_MATCH_THRESHOLD
    image_bytes = file.file.read()
    extracted = face_service.extract_embedding(image_bytes)
    if extracted is None:
        return RecognizeResponse(
            matched=False, threshold=threshold, reason="no_face_in_frame"
        )
    probe, _ = extracted

    gallery = _load_gallery(db, family_id)
    if not gallery:
        return RecognizeResponse(
            matched=False,
            threshold=threshold,
            reason="no_enrolled_embeddings",
        )
    result = face_service.match(probe, gallery, threshold=threshold)
    if result is None:
        return RecognizeResponse(
            matched=False,
            threshold=threshold,
            reason="below_threshold",
        )

    person = db.get(models.Person, result.person_id)
    return RecognizeResponse(
        matched=True,
        person_id=result.person_id,
        person_name=_display_name(person) if person else None,
        similarity=round(result.similarity, 4),
        threshold=threshold,
    )
