"""Background enrollment — keep ``face_embeddings`` in sync with photos.

Running InsightFace on a single photo typically takes 100–300 ms after
the model is warm but can burn 5–15 seconds on the very first call of a
worker process (model load + ONNX graph compile). We don't want photo
uploads to block on that, so every hook in this module is designed to be
scheduled through ``fastapi.BackgroundTasks`` and runs in its own DB
session (because the request-scoped session is already closed by the
time the task fires).

The hooks are **idempotent**: re-running them is always safe. That lets
the admin console and the manual ``/face/enroll`` endpoint both call into
the same code path without having to check for existing rows first.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .. import models, storage
from ..db import SessionLocal
from . import face as face_service

logger = logging.getLogger(__name__)


def enroll_photo(photo_id: int) -> None:
    """(Re)compute the embedding for one person_photo row.

    Safe to call repeatedly — if the photo is already enrolled we bail
    out early. Meant to be queued via ``BackgroundTasks.add_task``.
    """
    db = SessionLocal()
    try:
        photo = db.get(models.PersonPhoto, photo_id)
        if photo is None:
            logger.warning("enroll_photo: photo %s not found", photo_id)
            return
        if not photo.use_for_face_recognition:
            # Toggle flipped back off before the task ran; clean up any
            # embedding we might have written previously and exit.
            if _delete_embedding(db, photo_id):
                db.commit()
            return

        existing = db.execute(
            select(models.FaceEmbedding).where(
                models.FaceEmbedding.person_photo_id == photo_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        person = db.get(models.Person, photo.person_id)
        if person is None:
            logger.warning(
                "enroll_photo: person %s gone for photo %s",
                photo.person_id,
                photo_id,
            )
            return

        try:
            abs_path = storage.absolute_path(photo.stored_file_path)
            with open(abs_path, "rb") as fh:
                image_bytes = fh.read()
        except FileNotFoundError:
            logger.warning(
                "enroll_photo: stored file missing for photo %s (%s)",
                photo_id,
                photo.stored_file_path,
            )
            return

        try:
            extracted = face_service.extract_embedding(image_bytes)
        except Exception:  # noqa: BLE001
            logger.exception("enroll_photo: embedding failed for photo %s", photo_id)
            return

        if extracted is None:
            logger.info(
                "enroll_photo: no detectable face in photo %s (person %s)",
                photo_id,
                photo.person_id,
            )
            return

        emb, bbox = extracted
        row = models.FaceEmbedding(
            person_photo_id=photo_id,
            person_id=photo.person_id,
            family_id=person.family_id,
            model_name="buffalo_l",
            embedding_dim=int(emb.shape[0]),
            embedding_bytes=face_service.encode_bytes(emb),
            bounding_box_json=face_service.bbox_to_json(bbox),
        )
        db.add(row)
        db.commit()
        logger.info(
            "enroll_photo: embedded photo %s for person %s (family %s)",
            photo_id,
            photo.person_id,
            person.family_id,
        )
    except Exception:
        logger.exception("enroll_photo: unexpected error for photo %s", photo_id)
    finally:
        db.close()


def remove_photo_enrollment(photo_id: int) -> None:
    """Drop any embedding previously computed for this photo."""
    db = SessionLocal()
    try:
        deleted = _delete_embedding(db, photo_id)
        if deleted:
            db.commit()
            logger.info("remove_photo_enrollment: deleted embedding for photo %s", photo_id)
    finally:
        db.close()


def _delete_embedding(db, photo_id: int) -> bool:
    existing = db.execute(
        select(models.FaceEmbedding).where(
            models.FaceEmbedding.person_photo_id == photo_id
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    db.delete(existing)
    return True
