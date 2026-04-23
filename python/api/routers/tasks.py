"""HTTP routes for the family kanban task board.

URL layout (all under ``/api/admin``)::

    GET    /tasks?family_id=&status=&priority=&assigned_to=&task_kind=&q=&include_done=
    POST   /tasks
    GET    /tasks/{task_id}                # detail with comments / followers / attachments / links
    PATCH  /tasks/{task_id}
    DELETE /tasks/{task_id}

    PUT    /tasks/{task_id}/schedule       # cron edit + pause/resume (monitoring tasks)
    POST   /tasks/{task_id}/run-now        # fire a monitoring run immediately

    POST   /tasks/{task_id}/comments
    DELETE /tasks/{task_id}/comments/{comment_id}

    POST   /tasks/{task_id}/followers
    DELETE /tasks/{task_id}/followers/{person_id}

    POST   /tasks/{task_id}/attachments    # multipart upload
    GET    /tasks/{task_id}/attachments/{attachment_id}/download
    DELETE /tasks/{task_id}/attachments/{attachment_id}

    POST   /tasks/{task_id}/links          # AI-discovered or person-added URL
    DELETE /tasks/{task_id}/links/{link_id}

The AI assistant uses :mod:`api.ai.tools` directly against the ORM
rather than going through HTTP — keeping the in-process call cheap —
but the surface area here matches what the admin UI consumes 1:1 so a
future external integration can drive everything via REST.
"""

from __future__ import annotations

import logging
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
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import models, schemas, storage
from ..auth import (
    CurrentUser,
    require_admin,
    require_family_member,
    require_user,
)
from ..config import get_settings
from ..db import get_db
from ..services import cron_helpers


logger = logging.getLogger(__name__)


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
    link_count: Optional[int] = None,
) -> schemas.TaskRead:
    """Hand-built serialiser so the count fields can be supplied from
    a single grouped query (no N+1).

    Renders ``cron_description`` from ``cron_schedule`` on the way out
    so the client never has to parse cron itself — every monitoring
    list view gets a free "At 09:00 AM, every day" string.
    """
    cron_description: Optional[str] = None
    if task.cron_schedule:
        # describe() is best-effort and never raises, so this can't
        # break a list response if the user typed a malformed cron.
        cron_description = cron_helpers.describe(task.cron_schedule)

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
        owner_kind=task.owner_kind,  # type: ignore[arg-type]
        task_kind=task.task_kind,  # type: ignore[arg-type]
        cron_schedule=task.cron_schedule,
        cron_description=cron_description,
        next_run_at=task.next_run_at,
        last_run_at=task.last_run_at,
        last_run_status=task.last_run_status,  # type: ignore[arg-type]
        last_run_error=task.last_run_error,
        monitoring_paused=bool(task.monitoring_paused),
        follower_count=int(follower_count or 0),
        comment_count=int(comment_count or 0),
        attachment_count=int(attachment_count or 0),
        link_count=int(link_count or 0),
    )


def _get_task_or_404(db: Session, task_id: int) -> models.Task:
    task = db.get(models.Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _member_can_see_task(
    db: Session, task: models.Task, user: CurrentUser
) -> bool:
    """Member visibility rule: creator OR assignee OR explicit follower.

    Admins always see every task; this helper is only meaningful for
    role=member. Cross-family access is rejected even if the user
    happens to be a creator/assignee/follower (defence in depth — the
    schema doesn't enforce that ``created_by_person_id`` lives in the
    same family as ``family_id``, so we double-check).
    """
    if user.is_admin:
        return True
    if user.family_id != task.family_id or user.person_id is None:
        return False
    if task.created_by_person_id == user.person_id:
        return True
    if task.assigned_to_person_id == user.person_id:
        return True
    is_follower = (
        db.query(models.TaskFollower)
        .filter(
            models.TaskFollower.task_id == task.task_id,
            models.TaskFollower.person_id == user.person_id,
        )
        .first()
        is not None
    )
    return is_follower


def _require_visible_task(
    db: Session, task_id: int, user: CurrentUser
) -> models.Task:
    """Load a task and 404 if the current user can't see it.

    404 (not 403) so a member probing for arbitrary task IDs can't
    distinguish "doesn't exist" from "exists but you're not a follower".
    """
    task = _get_task_or_404(db, task_id)
    if not _member_can_see_task(db, task, user):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _resolve_family_timezone(db: Session, family_id: int) -> str:
    """Look up the family's IANA timezone, falling back to ET."""
    fam = db.get(models.Family, family_id)
    if fam is not None and getattr(fam, "timezone", None):
        return fam.timezone
    return "America/New_York"


def _validate_and_apply_schedule(
    db: Session,
    task: models.Task,
    *,
    cron_expression: Optional[str],
) -> None:
    """Validate ``cron_expression`` and update the task's schedule cols.

    Centralised so the create / patch / dedicated-schedule endpoints
    behave identically: same validation, same next_run_at recompute.
    Raises HTTP 422 on a bad cron string (the only reason cron parsing
    can fail in user input).
    """
    if cron_expression is None or not cron_expression.strip():
        # Clearing the schedule "pauses" auto-runs by leaving
        # next_run_at NULL. We don't change monitoring_paused here —
        # callers can do that explicitly.
        task.cron_schedule = None
        task.next_run_at = None
        return

    tz_name = _resolve_family_timezone(db, task.family_id)
    try:
        info = cron_helpers.parse(cron_expression, tz_name)
    except cron_helpers.CronError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    task.cron_schedule = info.expression
    # Only update next_run_at if the task is unpaused; a paused task
    # keeps next_run_at NULL so the scheduler scan skips it cheaply.
    task.next_run_at = None if task.monitoring_paused else info.next_run_utc


def _maybe_kick_off_first_run(task: models.Task) -> None:
    """Submit an immediate first run for an AI-owned monitoring task.

    Called after creation. Imports the scheduler service lazily to
    avoid a router→scheduler→router import cycle and to keep the
    router importable even if the scheduler module is broken.
    """
    if task.owner_kind != "ai" or task.task_kind != "monitoring":
        return
    if task.monitoring_paused:
        return
    try:
        from ..services import monitoring_scheduler

        monitoring_scheduler.run_now_in_background(task.task_id)
    except Exception:  # noqa: BLE001 - never let scheduling failure 500 the create
        logger.exception(
            "tasks.create: failed to kick off first run for task %d",
            task.task_id,
        )


# ---------------------------------------------------------------------------
# Tasks: list / create / read / update / delete
# ---------------------------------------------------------------------------


@router.get("", response_model=List[schemas.TaskRead])
def list_tasks(
    request: Request,
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
    task_kind: Optional[schemas.TaskKind] = Query(
        None,
        description=(
            "Limit to a single task shape: 'todo' for the kanban board, "
            "'monitoring' for the AI standing-job tab. Omit for both."
        ),
    ),
    owner_kind: Optional[schemas.TaskOwnerKind] = Query(
        None,
        description="Limit to 'human' or 'ai'-owned tasks.",
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
    user = require_family_member(family_id, request)
    stmt = select(models.Task).where(models.Task.family_id == family_id)
    if not user.is_admin and user.person_id is not None:
        # Members see only tasks they're personally involved in.
        # Use a single OR over creator / assignee / follower so the
        # query plan stays one index-per-leg + a small EXISTS for the
        # follower table; significantly cheaper than a UNION.
        stmt = stmt.where(
            or_(
                models.Task.created_by_person_id == user.person_id,
                models.Task.assigned_to_person_id == user.person_id,
                select(models.TaskFollower.task_follower_id)
                .where(models.TaskFollower.task_id == models.Task.task_id)
                .where(models.TaskFollower.person_id == user.person_id)
                .exists(),
            )
        )

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

    if task_kind is not None:
        stmt = stmt.where(models.Task.task_kind == task_kind)
    if owner_kind is not None:
        stmt = stmt.where(models.Task.owner_kind == owner_kind)

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
    link_counts = dict(
        db.execute(
            select(
                models.TaskLink.task_id,
                func.count(models.TaskLink.task_link_id),
            )
            .where(models.TaskLink.task_id.in_(ids))
            .group_by(models.TaskLink.task_id)
        ).all()
    )

    return [
        _serialize_task(
            r,
            follower_count=follower_counts.get(r.task_id, 0),
            comment_count=comment_counts.get(r.task_id, 0),
            attachment_count=attachment_counts.get(r.task_id, 0),
            link_count=link_counts.get(r.task_id, 0),
        )
        for r in rows
    ]


@router.post("", response_model=schemas.TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: schemas.TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> schemas.TaskRead:
    user = require_family_member(payload.family_id, request)
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    # Members can't impersonate other people. Force the creator to be
    # themselves regardless of what the client sent.
    if not user.is_admin and user.person_id is not None:
        payload.created_by_person_id = user.person_id

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

    # AI monitoring tasks have a different shape from human todos —
    # the assignee is meaningless (Avi is the owner) so we clear it,
    # and a missing cron string is filled with the configured default.
    is_ai_monitoring = (
        payload.owner_kind == "ai" and payload.task_kind == "monitoring"
    )
    settings = get_settings()
    cron_to_apply: Optional[str] = payload.cron_schedule
    assigned_to_person_id = payload.assigned_to_person_id
    if is_ai_monitoring:
        assigned_to_person_id = None
        if not cron_to_apply or not cron_to_apply.strip():
            cron_to_apply = settings.AI_MONITORING_DEFAULT_CRON

    task = models.Task(
        family_id=payload.family_id,
        created_by_person_id=payload.created_by_person_id,
        assigned_to_person_id=assigned_to_person_id,
        title=payload.title.strip(),
        description=payload.description,
        status=payload.status,
        priority=payload.priority,
        start_date=payload.start_date,
        desired_end_date=payload.desired_end_date,
        end_date=payload.end_date,
        completed_at=_now() if payload.status == "done" else None,
        owner_kind=payload.owner_kind,
        task_kind=payload.task_kind,
        monitoring_paused=payload.monitoring_paused,
    )
    # Validate cron + compute next_run_at against the family's tz. We
    # do this on the un-committed task so a bad cron 422s before any
    # row is written.
    _validate_and_apply_schedule(db, task, cron_expression=cron_to_apply)

    db.add(task)
    db.flush()

    # Followers — de-dup against creator/assignee since those are
    # implicit subscribers; the table only carries the EXTRAS.
    implicit = {
        i
        for i in (payload.created_by_person_id, assigned_to_person_id)
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

    # Kick off the first monitoring run AFTER the row is fully
    # committed so the background worker can read it. We commit
    # explicitly rather than relying on the request lifecycle so the
    # background thread sees the row immediately.
    if is_ai_monitoring and not task.monitoring_paused:
        db.commit()
        _maybe_kick_off_first_run(task)

    return _serialize_task(task, follower_count=len(task.followers))


@router.get("/{task_id}", response_model=schemas.TaskDetail)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> schemas.TaskDetail:
    task = (
        db.query(models.Task)
        .options(
            selectinload(models.Task.followers),
            selectinload(models.Task.comments),
            selectinload(models.Task.attachments),
            selectinload(models.Task.links),
        )
        .filter(models.Task.task_id == task_id)
        .first()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _member_can_see_task(db, task, user):
        raise HTTPException(status_code=404, detail="Task not found")
    return schemas.TaskDetail(
        **_serialize_task(
            task,
            follower_count=len(task.followers),
            comment_count=len(task.comments),
            attachment_count=len(task.attachments),
            link_count=len(task.links),
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
        links=[schemas.TaskLinkRead.model_validate(link) for link in task.links],
    )


@router.patch("/{task_id}", response_model=schemas.TaskRead)
def update_task(
    task_id: int,
    payload: schemas.TaskUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> schemas.TaskRead:
    task = _require_visible_task(db, task_id, user)
    changes = payload.model_dump(exclude_unset=True)

    # Members can update visible tasks but cannot reassign ownership
    # or change identity-shaping fields. Silently drop those keys
    # rather than 403'ing — the UI hides the controls anyway and we
    # don't want a clumsy client to dead-end on a partial save.
    if not user.is_admin:
        for forbidden in (
            "assigned_to_person_id",
            "created_by_person_id",
            "owner_kind",
            "task_kind",
            "monitoring_paused",
            "cron_schedule",
        ):
            changes.pop(forbidden, None)

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

    # Pull cron-related fields out of the generic setattr loop so we
    # can re-validate + recompute next_run_at atomically.
    cron_change = changes.pop("cron_schedule", "<unset>")
    pause_change = changes.pop("monitoring_paused", "<unset>")

    for field, value in changes.items():
        setattr(task, field, value)

    if pause_change != "<unset>":
        task.monitoring_paused = bool(pause_change)
    if cron_change != "<unset>":
        _validate_and_apply_schedule(db, task, cron_expression=cron_change)
    elif pause_change != "<unset>":
        # Schedule didn't change but pause did; if we just unpaused
        # and there's a cron, recompute next_run from "now".
        if (
            not task.monitoring_paused
            and task.cron_schedule
            and task.next_run_at is None
        ):
            tz_name = _resolve_family_timezone(db, task.family_id)
            try:
                task.next_run_at = cron_helpers.next_run(
                    task.cron_schedule, tz_name
                )
            except cron_helpers.CronError:
                # Stored cron somehow became invalid; leave next_run
                # null so the scheduler skips it and surface the cron
                # string to the UI for the user to fix.
                task.next_run_at = None
        elif task.monitoring_paused:
            task.next_run_at = None

    db.flush()
    db.refresh(task)

    counts = _counts_for(db, task.task_id)
    return _serialize_task(task, **counts)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
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
        "link_count": db.scalar(
            select(func.count(models.TaskLink.task_link_id)).where(
                models.TaskLink.task_id == task_id
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
    user: CurrentUser = Depends(require_user),
) -> models.TaskComment:
    task = _require_visible_task(db, task_id, user)
    # Members can only post AS themselves. Force author_person_id
    # to their own person row regardless of what the client sent.
    if not user.is_admin and user.person_id is not None:
        payload.author_kind = "person"
        payload.author_person_id = user.person_id
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
    dependencies=[Depends(require_admin)],
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
    dependencies=[Depends(require_admin)],
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
    dependencies=[Depends(require_admin)],
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
    user: CurrentUser = Depends(require_user),
) -> models.TaskAttachment:
    task = _require_visible_task(db, task_id, user)
    # Members can only attach AS themselves.
    if not user.is_admin and user.person_id is not None:
        uploaded_by_person_id = user.person_id
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
    task_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    _require_visible_task(db, task_id, user)
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
    dependencies=[Depends(require_admin)],
)
def delete_attachment(
    task_id: int, attachment_id: int, db: Session = Depends(get_db)
) -> None:
    att = db.get(models.TaskAttachment, attachment_id)
    if att is None or att.task_id != task_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    storage.delete_if_exists(att.stored_file_path)
    db.delete(att)


# ---------------------------------------------------------------------------
# Schedule + run-now (monitoring tasks)
# ---------------------------------------------------------------------------


@router.put(
    "/{task_id}/schedule",
    response_model=schemas.TaskRead,
    dependencies=[Depends(require_admin)],
)
def update_schedule(
    task_id: int,
    payload: schemas.TaskScheduleUpdate,
    db: Session = Depends(get_db),
) -> schemas.TaskRead:
    """Edit the cron schedule and/or pause flag on a monitoring task.

    Separated from :func:`update_task` so the UI can wire a cron
    editor to a single endpoint that atomically validates the cron
    expression AND recomputes ``next_run_at`` against the family's
    timezone. A bad cron returns 422 with a readable error.
    """
    task = _get_task_or_404(db, task_id)
    if task.task_kind != "monitoring":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Only monitoring tasks have a cron schedule. Convert "
                "the task to task_kind='monitoring' first."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if "monitoring_paused" in changes:
        task.monitoring_paused = bool(changes["monitoring_paused"])

    if "cron_schedule" in changes:
        _validate_and_apply_schedule(
            db, task, cron_expression=changes["cron_schedule"]
        )
    elif "monitoring_paused" in changes:
        # Schedule untouched but pause toggled — recompute or clear
        # next_run_at to match the new pause state.
        if task.monitoring_paused:
            task.next_run_at = None
        elif task.cron_schedule and task.next_run_at is None:
            tz_name = _resolve_family_timezone(db, task.family_id)
            try:
                task.next_run_at = cron_helpers.next_run(
                    task.cron_schedule, tz_name
                )
            except cron_helpers.CronError:
                task.next_run_at = None

    db.flush()
    db.refresh(task)
    counts = _counts_for(db, task.task_id)
    return _serialize_task(task, **counts)


@router.post(
    "/{task_id}/run-now",
    response_model=schemas.TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin)],
)
def run_now(task_id: int, db: Session = Depends(get_db)) -> schemas.TaskRead:
    """Fire a monitoring run for this task right now, in the background.

    Useful for "Run now" buttons in the UI and for the
    'I just edited the cron, let me see results' workflow. The agent
    work is submitted to the shared background pool — this endpoint
    returns immediately with the (still-running) task row. Poll the
    detail endpoint to see comments / links materialise.
    """
    task = _get_task_or_404(db, task_id)
    if task.task_kind != "monitoring" or task.owner_kind != "ai":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Only AI-owned monitoring tasks can be run via this "
                "endpoint."
            ),
        )

    try:
        from ..services import monitoring_scheduler

        monitoring_scheduler.run_now_in_background(task.task_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "tasks.run_now: failed to submit task %d", task.task_id
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start run: {exc}",
        ) from exc

    db.refresh(task)
    counts = _counts_for(db, task.task_id)
    return _serialize_task(task, **counts)


# ---------------------------------------------------------------------------
# Links (AI-discovered or person-added URL citations)
# ---------------------------------------------------------------------------


@router.post(
    "/{task_id}/links",
    response_model=schemas.TaskLinkRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def add_link(
    task_id: int,
    payload: schemas.TaskLinkCreate,
    db: Session = Depends(get_db),
) -> models.TaskLink:
    task = _get_task_or_404(db, task_id)

    if (
        payload.added_by_kind == "person"
        and payload.added_by_person_id is not None
        and db.get(models.Person, payload.added_by_person_id) is None
    ):
        raise HTTPException(status_code=404, detail="Author person not found")

    # Idempotent on (task_id, url) so the AI re-citing the same source
    # on a follow-up run doesn't 500 — return the existing row.
    existing = db.execute(
        select(models.TaskLink)
        .where(models.TaskLink.task_id == task.task_id)
        .where(models.TaskLink.url == payload.url)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    link = models.TaskLink(
        task_id=task.task_id,
        url=payload.url.strip(),
        title=(payload.title or "").strip() or None,
        summary=payload.summary,
        added_by_kind=payload.added_by_kind,
        added_by_person_id=(
            payload.added_by_person_id
            if payload.added_by_kind == "person"
            else None
        ),
        created_at=_now(),
    )
    db.add(link)
    db.flush()
    db.refresh(link)
    return link


@router.delete(
    "/{task_id}/links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_link(
    task_id: int, link_id: int, db: Session = Depends(get_db)
) -> None:
    link = db.get(models.TaskLink, link_id)
    if link is None or link.task_id != task_id:
        raise HTTPException(status_code=404, detail="Link not found")
    db.delete(link)
