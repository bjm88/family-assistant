"""Read endpoints for the AI agent audit trail.

The agent loop itself is driven by ``/api/aiassistant/chat`` (and, in
the future, a background worker). These endpoints are pure read
helpers that let the UI build a "Tasks" history page and replay any
single task's steps for debugging.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..auth import require_admin
from ..db import get_db


# Agent audit trail — admin only. The agent runs in Avi's voice, but
# the step replay can include cross-family context, so members never
# see this surface.
router = APIRouter(
    prefix="/aiassistant/tasks",
    tags=["agent_tasks"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StepRead(BaseModel):
    agent_step_id: int
    step_index: int
    step_type: str
    tool_name: Optional[str] = None
    # Tool inputs are always JSON objects but tool outputs can also be
    # lists (lookup_person returns a list of people, calendar events are
    # a list, etc.) so we accept any JSON-serialisable shape here.
    tool_input: Optional[dict] = None
    tool_output: Optional[Any] = None
    content: Optional[str] = None
    error: Optional[str] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TaskSummary(BaseModel):
    agent_task_id: int
    family_id: int
    live_session_id: Optional[int] = None
    person_id: Optional[int] = None
    kind: str
    status: str
    input_text: str
    summary: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    model: Optional[str] = None
    created_at: datetime
    step_count: int

    class Config:
        from_attributes = True


class TaskDetail(TaskSummary):
    steps: List[StepRead] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[TaskSummary])
def list_tasks(
    family_id: int = Query(..., description="Scope to this family."),
    limit: int = Query(50, ge=1, le=200),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Optional status filter (running, succeeded, failed, …).",
    ),
    db: Session = Depends(get_db),
) -> List[TaskSummary]:
    """Recent agent tasks for the family, newest first."""
    q = (
        db.query(models.AgentTask)
        .filter(models.AgentTask.family_id == family_id)
        .order_by(models.AgentTask.created_at.desc())
    )
    if status_filter:
        q = q.filter(models.AgentTask.status == status_filter)
    rows = q.limit(limit).all()

    # Cheap step-count via a single grouped query — beats N+1 lazy loads.
    if rows:
        ids = [r.agent_task_id for r in rows]
        from sqlalchemy import func, select

        counts = dict(
            db.execute(
                select(
                    models.AgentStep.agent_task_id,
                    func.count(models.AgentStep.agent_step_id),
                )
                .where(models.AgentStep.agent_task_id.in_(ids))
                .group_by(models.AgentStep.agent_task_id)
            ).all()
        )
    else:
        counts = {}

    return [
        TaskSummary(
            agent_task_id=r.agent_task_id,
            family_id=r.family_id,
            live_session_id=r.live_session_id,
            person_id=r.person_id,
            kind=r.kind,
            status=r.status,
            input_text=r.input_text,
            summary=r.summary,
            error=r.error,
            started_at=r.started_at,
            completed_at=r.completed_at,
            duration_ms=r.duration_ms,
            model=r.model,
            created_at=r.created_at,
            step_count=int(counts.get(r.agent_task_id, 0)),
        )
        for r in rows
    ]


@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: int, db: Session = Depends(get_db)) -> TaskDetail:
    """Full transcript for a single agent task."""
    row = (
        db.query(models.AgentTask)
        .options(selectinload(models.AgentTask.steps))
        .filter(models.AgentTask.agent_task_id == task_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return TaskDetail(
        agent_task_id=row.agent_task_id,
        family_id=row.family_id,
        live_session_id=row.live_session_id,
        person_id=row.person_id,
        kind=row.kind,
        status=row.status,
        input_text=row.input_text,
        summary=row.summary,
        error=row.error,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        model=row.model,
        created_at=row.created_at,
        step_count=len(row.steps),
        steps=[StepRead.model_validate(s) for s in row.steps],
    )
