"""HTTP routes for the family kanban task board.

URL layout (all under ``/api/admin``)::

    GET    /tasks?family_id=&status=&priority=&assigned_to=&q=&include_done=
    POST   /tasks
    GET    /tasks/{task_id}                # detail with comments / followers / attachments
    PATCH  /tasks/{task_id}
    DELETE /tasks/{task_id}

    POST   /tasks/{task_id}/comments
    DELETE /tasks/{task_id}/comments/{comment_id}

    POST   /tasks/{task_id}/followers
    DELETE /tasks/{task_id}/followers/{person_id}

    POST   /tasks/{task_id}/attachments    # multipart upload
    GET    /tasks/{task_id}/attachments/{attachment_id}/download
    DELETE /tasks/{task_id}/attachments/{attachment_id}

The AI assistant uses :mod:`api.ai.tools` directly against the ORM
rather than going through HTTP — keeping the in-process call cheap —
but the surface area here matches what the admin UI consumes 1:1 so a
future external integration can drive everything via REST.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
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
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import models, schemas, storage
from ..db import get_db


router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kind_for_mime(mime: Optional[str]) -> str:
    """Pick a coarse :data:`models.TASK_ATTACHMENT_KINDS` label for a mime type.

    The kanban card uses this to decide between rendering a thumbnail
    (photo) vs a chip (pdf / document). Free-form mime stays in the
    ``mime_type`` column for download dispatch.
    """
    if not mime:
        return "document"
    if mime.startswith("image/"):
        return "photo"
    if mime == "application/pdf":
        return "pdf"
    return "document"


def _serialize_task(
    task: models.Task,
    *,
    follower_count: Optional[int] = None,
    comment_count: Optional[int] = None,
    attachment_count: Optional[int] = None,
) -> schemas.TaskRead:
    """Hand-built serialiser so the count fields can be supplied from
    a single grouped query (no N+1)."""
    return schemas.TaskRead(
        task_id=task.task_id,
        family_id=task.family_id,
        created_by_person_id=task.created_by_person_id,
        assigned_to_person_id=task.assigned_to_person_id,
        title=task.title,
        description=task.description,
        status=task.status,  # type: ignore[arg-type]
        priority=task.priority,  # type: ignore[arg-type]
        start_date=task.start_date,
        desired_end_date=task.desired_end_date,
        end_date=task.end_date,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        follower_count=int(follower_count or 0),
        comment_count=int(comment_count or 0),
        attachment_count=int(attachment_count or 0),
    )


def _get_task_or_404(db: Session, task_id: int) -> models.Task:
    task = db.get(models.Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ---------------------------------------------------------------------------
# Tasks: list / create / read / update / delete
# ---------------------------------------------------------------------------


@router.get("", response_model=List[schemas.TaskRead])
def list_tasks(
    family_id: int = Query(..., description="Scope to this family."),
    status_filter: Optional[schemas.TaskStatus] = Query(
        None,
        alias="status",
        description="Limit to a single kanban column.",
    ),
    priority: Optional[schemas.TaskPriority] = Query(
        None, description="Limit to a single priority bucket."
    ),
    assigned_to: Optional[int] = Query(
        None,
        description=(
            "Person id of the assignee. Use a sentinel value of 0 to "
            "filter to UNASSIGNED tasks (assigned_to_person_id IS NULL)."
        ),
    ),
    created_by: Optional[int] = Query(
        None, description="Person id of the creator."
    ),
    q: Optional[str] = Query(
        None,
        description=(
            "Case-insensitive substring match against title OR "
            "description. Powered by ILIKE — fine at our scale."
        ),
    ),
    include_done: bool = Query(
        True,
        description=(
            "Include tasks already in status='done'. Defaults to true so "
            "the kanban board can show the closed column; the AI can pass "
            "false to focus on live work only."
        ),
    ),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> List[schemas.TaskRead]:
    """Tasks for the family, sorted to put urgent / open work first.

    The board orders by priority severity, then by ``created_at``
    descending. Done tasks always sink to the bottom regardless of
    priority so a stale 'urgent' that's already closed doesn't crowd
    the active items.
    """
    stmt = select(models.Task).where(models.Task.family_id == family_id)

    if status_filter is not None:
        stmt = stmt.where(models.Task.status == status_filter)
    elif not include_done:
        stmt = stmt.where(models.Task.status != "done")

    if priority is not None:
        stmt = stmt.where(models.Task.priority == priority)

    if assigned_to is not None:
        if int(assigned_to) == 0:
            stmt = stmt.where(models.Task.assigned_to_person_id.is_(None))
        else:
            stmt = stmt.where(
                models.Task.assigned_to_person_id == int(assigned_to)
            )
    if created_by is not None:
        stmt = stmt.where(models.Task.created_by_person_id == int(created_by))

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                models.Task.title.ilike(like),
                models.Task.description.ilike(like),
            )
        )

    # Postgres sort order: 'done' last, then priority severity, then
    # newest first. We use a CASE expression to map both enum-ish
    # columns to integer sort keys without an extra schema migration.
    status_rank = case(
        (models.Task.status == "in_progress", 0),
        (models.Task.status == "finalizing", 1),
        (models.Task.status == "new", 2),
        (models.Task.status == "done", 9),
        else_=5,
    )
    priority_rank = case(
        (models.Task.priority == "urgent", 0),
        (models.Task.priority == "high", 1),
        (models.Task.priority == "normal", 2),
        (models.Task.priority == "low", 3),
        (models.Task.priority == "future_idea", 4),
        else_=5,
    )
    stmt = stmt.order_by(
        status_rank.asc(),
        priority_rank.asc(),
        models.Task.created_at.desc(),
    ).limit(limit)

    rows = list(db.execute(stmt).scalars())
    if not rows:
        return []

    ids = [r.task_id for r in rows]

    follower_counts = dict(
        db.execute(
            select(
                models.TaskFollower.task_id,
                func.count(models.TaskFollower.task_follower_id),
            )
            .where(models.TaskFollower.task_id.in_(ids))
            .group_by(models.TaskFollower.task_id)
        ).all()
    )
    comment_counts = dict(
        db.execute(
            select(
                models.TaskComment.task_id,
                func.count(models.TaskComment.task_comment_id),
            )
            .where(models.TaskComment.task_id.in_(ids))
            .group_by(models.TaskComment.task_id)
        ).all()
    )
    attachment_counts = dict(
        db.execute(
            select(
                models.TaskAttachment.task_id,
                func.count(models.TaskAttachment.task_attachment_id),
            )
            .where(models.TaskAttachment.task_id.in_(ids))
            .group_by(models.TaskAttachment.task_id)
        ).all()
    )

    return [
        _serialize_task(
            r,
            follower_count=follower_counts.get(r.task_id, 0),
            comment_count=comment_counts.get(r.task_id, 0),
            attachment_count=attachment_counts.get(r.task_id, 0),
        )
        for r in rows
    ]


@router.post("", response_model=schemas.TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: schemas.TaskCreate, db: Session = Depends(get_db)
) -> schemas.TaskRead:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    if (
        payload.created_by_person_id is not None
        and db.get(models.Person, payload.created_by_person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Creator person not found")

    if (
        payload.assigned_to_person_id is not None
        and db.get(models.Person, payload.assigned_to_person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Assignee person not found")

    task = models.Task(
        family_id=payload.family_id,
        created_by_person_id=payload.created_by_person_id,
        assigned_to_person_id=payload.assigned_to_person_id,
        title=payload.title.strip(),
        description=payload.description,
        status=payload.status,
        priority=payload.priority,
        start_date=payload.start_date,
        desired_end_date=payload.desired_end_date,
        end_date=payload.end_date,
        completed_at=_now() if payload.status == "done" else None,
    )
    db.add(task)
    db.flush()

    # Followers — de-dup against creator/assignee since those are
    # implicit subscribers; the table only carries the EXTRAS.
    implicit = {
        i
        for i in (payload.created_by_person_id, payload.assigned_to_person_id)
        if i is not None
    }
    for pid in payload.follower_person_ids:
        if pid in implicit:
            continue
        if db.get(models.Person, pid) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Follower person {pid} not found",
            )
        db.add(
            models.TaskFollower(
                task_id=task.task_id, person_id=pid, added_at=_now()
            )
        )
        implicit.add(pid)

    db.flush()
    db.refresh(task)
    return _serialize_task(task, follower_count=len(task.followers))


@router.get("/{task_id}", response_model=schemas.TaskDetail)
def get_task(task_id: int, db: Session = Depends(get_db)) -> schemas.TaskDetail:
    task = (
        db.query(models.Task)
        .options(
            selectinload(models.Task.followers),
            selectinload(models.Task.comments),
            selectinload(models.Task.attachments),
        )
        .filter(models.Task.task_id == task_id)
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return schemas.TaskDetail(
        **_serialize_task(
            task,
            follower_count=len(task.followers),
            comment_count=len(task.comments),
            attachment_count=len(task.attachments),
        ).model_dump(),
        followers=[
            schemas.TaskFollowerRead.model_validate(f) for f in task.followers
        ],
        comments=[
            schemas.TaskCommentRead.model_validate(c) for c in task.comments
        ],
        attachments=[
            schemas.TaskAttachmentRead.model_validate(a)
            for a in task.attachments
        ],
    )


@router.patch("/{task_id}", response_model=schemas.TaskRead)
def update_task(
    task_id: int,
    payload: schemas.TaskUpdate,
    db: Session = Depends(get_db),
) -> schemas.TaskRead:
    task = _get_task_or_404(db, task_id)
    changes = payload.model_dump(exclude_unset=True)

    if "assigned_to_person_id" in changes and changes["assigned_to_person_id"] is not None:
        if db.get(models.Person, changes["assigned_to_person_id"]) is None:
            raise HTTPException(status_code=404, detail="Assignee person not found")

    new_status = changes.get("status")
    if new_status is not None and new_status != task.status:
        # Auto-stamp completed_at when entering 'done'; clear it when
        # leaving so a re-opened task doesn't lie about being closed.
        if new_status == "done" and task.completed_at is None:
            task.completed_at = _now()
        elif new_status != "done" and task.completed_at is not None:
            task.completed_at = None

    for field, value in changes.items():
        setattr(task, field, value)

    db.flush()
    db.refresh(task)

    counts = _counts_for(db, task.task_id)
    return _serialize_task(task, **counts)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db)) -> None:
    task = _get_task_or_404(db, task_id)
    # Clean up attachment files on disk before the cascade deletes the
    # rows. Failure to unlink an individual file is non-fatal — the row
    # going away is what matters for correctness.
    for att in list(task.attachments):
        storage.delete_if_exists(att.stored_file_path)
    db.delete(task)


def _counts_for(db: Session, task_id: int) -> dict:
    return {
        "follower_count": db.scalar(
            select(func.count(models.TaskFollower.task_follower_id)).where(
                models.TaskFollower.task_id == task_id
            )
        )
        or 0,
        "comment_count": db.scalar(
            select(func.count(models.TaskComment.task_comment_id)).where(
                models.TaskComment.task_id == task_id
            )
        )
        or 0,
        "attachment_count": db.scalar(
            select(func.count(models.TaskAttachment.task_attachment_id)).where(
                models.TaskAttachment.task_id == task_id
            )
        )
        or 0,
    }


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/comments",
    response_model=schemas.TaskCommentRead,
    status_code=status.HTTP_201_CREATED,
)
def add_comment(
    task_id: int,
    payload: schemas.TaskCommentCreate,
    db: Session = Depends(get_db),
) -> models.TaskComment:
    task = _get_task_or_404(db, task_id)
    if (
        payload.author_kind == "person"
        and payload.author_person_id is not None
        and db.get(models.Person, payload.author_person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Author person not found")

    comment = models.TaskComment(
        task_id=task.task_id,
        author_person_id=(
            payload.author_person_id if payload.author_kind == "person" else None
        ),
        author_kind=payload.author_kind,
        body=payload.body,
        created_at=_now(),
    )
    db.add(comment)
    db.flush()
    db.refresh(comment)
    return comment


@router.delete(
    "/{task_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_comment(
    task_id: int, comment_id: int, db: Session = Depends(get_db)
) -> None:
    comment = db.get(models.TaskComment, comment_id)
    if comment is None or comment.task_id != task_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    db.delete(comment)


# ---------------------------------------------------------------------------
# Followers
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/followers",
    response_model=schemas.TaskFollowerRead,
    status_code=status.HTTP_201_CREATED,
)
def add_follower(
    task_id: int,
    payload: schemas.TaskFollowerCreate,
    db: Session = Depends(get_db),
) -> models.TaskFollower:
    task = _get_task_or_404(db, task_id)
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    existing = db.execute(
        select(models.TaskFollower)
        .where(models.TaskFollower.task_id == task.task_id)
        .where(models.TaskFollower.person_id == payload.person_id)
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: re-attaching a follower returns the existing row
        # instead of crashing on the unique constraint. The UI sometimes
        # double-fires the click and we don't want a noisy error.
        return existing

    follower = models.TaskFollower(
        task_id=task.task_id,
        person_id=payload.person_id,
        added_at=_now(),
    )
    db.add(follower)
    db.flush()
    db.refresh(follower)
    return follower


@router.delete(
    "/{task_id}/followers/{person_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_follower(
    task_id: int, person_id: int, db: Session = Depends(get_db)
) -> None:
    follower = db.execute(
        select(models.TaskFollower)
        .where(models.TaskFollower.task_id == task_id)
        .where(models.TaskFollower.person_id == person_id)
    ).scalar_one_or_none()
    if follower is None:
        raise HTTPException(status_code=404, detail="Follower not found")
    db.delete(follower)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/attachments",
    response_model=schemas.TaskAttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_attachment(
    task_id: int,
    file: UploadFile = File(...),
    uploaded_by_person_id: Optional[int] = Form(None),
    caption: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> models.TaskAttachment:
    task = _get_task_or_404(db, task_id)
    if (
        uploaded_by_person_id is not None
        and db.get(models.Person, uploaded_by_person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Uploader person not found")

    rel_path, size, mime = storage.save_task_attachment(
        task.family_id,
        task.task_id,
        file.file,
        file.filename or "upload.bin",
    )

    attachment = models.TaskAttachment(
        task_id=task.task_id,
        uploaded_by_person_id=uploaded_by_person_id,
        attachment_kind=_kind_for_mime(mime),
        stored_file_path=rel_path,
        original_file_name=file.filename or "upload.bin",
        mime_type=mime,
        file_size_bytes=size,
        caption=caption,
        created_at=_now(),
    )
    db.add(attachment)
    db.flush()
    db.refresh(attachment)
    return attachment


@router.get("/{task_id}/attachments/{attachment_id}/download")
def download_attachment(
    task_id: int, attachment_id: int, db: Session = Depends(get_db)
):
    att = db.get(models.TaskAttachment, attachment_id)
    if att is None or att.task_id != task_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = storage.absolute_path(att.stored_file_path)
    if not path.exists():
        raise HTTPException(
            status_code=410, detail="Attachment file is missing on disk."
        )
    media = att.mime_type or mimetypes.guess_type(att.original_file_name)[0]
    return FileResponse(
        path,
        media_type=media or "application/octet-stream",
        filename=att.original_file_name,
    )


@router.delete(
    "/{task_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_attachment(
    task_id: int, attachment_id: int, db: Session = Depends(get_db)
) -> None:
    att = db.get(models.TaskAttachment, attachment_id)
    if att is None or att.task_id != task_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    storage.delete_if_exists(att.stored_file_path)
    db.delete(att)
