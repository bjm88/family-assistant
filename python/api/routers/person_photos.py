from __future__ import annotations

from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..ai.enrollment import enroll_photo, remove_photo_enrollment
from ..auth import CurrentUser, require_admin, require_user
from ..db import get_db

router = APIRouter(prefix="/person-photos", tags=["person_photos"])


@router.get("", response_model=List[schemas.PersonPhotoRead])
def list_person_photos(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> List[models.PersonPhoto]:
    # Members must scope by person_id, and the person must live in
    # their family. Admins may enumerate freely.
    if not user.is_admin:
        if person_id is None:
            raise HTTPException(
                status_code=403,
                detail="person_id is required for non-admin users.",
            )
        person = db.get(models.Person, person_id)
        if person is None or person.family_id != user.family_id:
            raise HTTPException(status_code=404, detail="Person not found")
    stmt = select(models.PersonPhoto).order_by(models.PersonPhoto.created_at.desc())
    if person_id is not None:
        stmt = stmt.where(models.PersonPhoto.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=schemas.PersonPhotoRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def upload_person_photo(
    background: BackgroundTasks,
    person_id: int = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    use_for_face_recognition: bool = Form(True),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.PersonPhoto:
    person = db.get(models.Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Person photos must be images.")

    rel_path, size, mime = storage.save_person_photo(
        person.family_id, person.person_id, file.file, file.filename or "photo.jpg"
    )
    photo = models.PersonPhoto(
        person_id=person.person_id,
        title=title,
        description=description,
        use_for_face_recognition=use_for_face_recognition,
        stored_file_path=rel_path,
        original_file_name=file.filename or "photo.jpg",
        mime_type=mime,
        file_size_bytes=size,
    )
    db.add(photo)
    db.flush()
    db.refresh(photo)

    # Fire-and-forget: the InsightFace pass is slow on the first call of a
    # worker and we don't want uploads to block on it. The live AI page
    # polls /face/status so the new embedding shows up automatically.
    if use_for_face_recognition:
        # Explicitly commit here so the background task (which opens its
        # own fresh Session) can see the newly-inserted photo row. The
        # request-scoped session would otherwise only commit in get_db's
        # cleanup, which happens AFTER background tasks fire.
        db.commit()
        db.refresh(photo)
        background.add_task(enroll_photo, photo.person_photo_id)

    return photo


@router.patch(
    "/{person_photo_id}",
    response_model=schemas.PersonPhotoRead,
    dependencies=[Depends(require_admin)],
)
def update_person_photo(
    person_photo_id: int,
    payload: schemas.PersonPhotoUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> models.PersonPhoto:
    photo = db.get(models.PersonPhoto, person_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    # Remember whether the photo WAS enrolled so we can decide whether to
    # schedule an embedding or a cleanup after the update is applied.
    prev_flag = bool(photo.use_for_face_recognition)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(photo, field, value)
    db.flush()
    db.refresh(photo)

    new_flag = bool(photo.use_for_face_recognition)
    if new_flag != prev_flag:
        # Same story as the POST path — commit before scheduling so the
        # task's fresh session observes the updated flag.
        db.commit()
        db.refresh(photo)
        if new_flag:
            background.add_task(enroll_photo, photo.person_photo_id)
        else:
            background.add_task(remove_photo_enrollment, photo.person_photo_id)
    # If the flag stayed on and stayed on, the embedding is already good.
    # Replacing the image bytes goes through a separate upload (DELETE +
    # POST), which naturally re-enrolls via the POST path.

    return photo


@router.delete(
    "/{person_photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_person_photo(person_photo_id: int, db: Session = Depends(get_db)) -> None:
    photo = db.get(models.PersonPhoto, person_photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    # The FK on face_embeddings.person_photo_id has ON DELETE CASCADE, so
    # the embedding row is cleaned up automatically when the photo is
    # deleted — no background task needed here.
    storage.delete_if_exists(photo.stored_file_path)
    db.delete(photo)
