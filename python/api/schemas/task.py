"""Pydantic schemas for the family task board.

These mirror :mod:`api.models.task` and are kept intentionally close to
the ORM shape — the kanban UI consumes ``TaskRead`` directly. The only
"computed" fields are ``follower_count`` / ``comment_count`` /
``attachment_count`` so a list view can render badges without a second
round trip; the detail view inlines the actual children via
``TaskDetail``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


# Mirror the constants from models/task.py so a typo on either side
# fails the type check rather than silently passing through.
TaskStatus = Literal["new", "in_progress", "finalizing", "done"]
TaskPriority = Literal["urgent", "high", "normal", "low", "future_idea"]
TaskCommentAuthorKind = Literal["person", "assistant"]
TaskAttachmentKind = Literal["photo", "pdf", "document", "other"]
TaskOwnerKind = Literal["human", "ai"]
TaskKind = Literal["todo", "monitoring"]
TaskLastRunStatus = Literal["ok", "error", "running"]
TaskLinkAddedByKind = Literal["assistant", "person"]


# ---------------------------------------------------------------------------
# Comments / followers / attachments
# ---------------------------------------------------------------------------


class TaskCommentRead(OrmModel):
    task_comment_id: int
    task_id: int
    author_person_id: Optional[int]
    author_kind: TaskCommentAuthorKind
    body: str
    created_at: datetime


class TaskCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)
    author_person_id: Optional[int] = Field(
        default=None,
        description=(
            "Author when the speaker is a household member. Leave NULL "
            "and set author_kind='assistant' for Avi-authored notes."
        ),
    )
    author_kind: TaskCommentAuthorKind = "person"


class TaskFollowerRead(OrmModel):
    task_follower_id: int
    task_id: int
    person_id: int
    added_at: datetime


class TaskFollowerCreate(BaseModel):
    person_id: int


class TaskAttachmentRead(OrmModel):
    task_attachment_id: int
    task_id: int
    uploaded_by_person_id: Optional[int]
    attachment_kind: TaskAttachmentKind
    original_file_name: str
    mime_type: Optional[str]
    file_size_bytes: Optional[int]
    caption: Optional[str]
    created_at: datetime


class TaskLinkRead(OrmModel):
    task_link_id: int
    task_id: int
    url: str
    title: Optional[str]
    summary: Optional[str]
    added_by_kind: TaskLinkAddedByKind
    added_by_person_id: Optional[int]
    created_at: datetime


class TaskLinkCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    title: Optional[str] = Field(default=None, max_length=500)
    summary: Optional[str] = None
    added_by_kind: TaskLinkAddedByKind = "person"
    added_by_person_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Task itself
# ---------------------------------------------------------------------------


class TaskBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    status: TaskStatus = "new"
    priority: TaskPriority = "normal"
    assigned_to_person_id: Optional[int] = None
    start_date: Optional[date] = None
    desired_end_date: Optional[date] = None
    end_date: Optional[date] = None
    # Owner / shape — defaults match the original kanban behaviour so
    # existing clients don't need to send these.
    owner_kind: TaskOwnerKind = "human"
    task_kind: TaskKind = "todo"
    cron_schedule: Optional[str] = Field(
        default=None,
        max_length=120,
        description=(
            "Standard 5-field cron expression. Required when "
            "owner_kind='ai' AND task_kind='monitoring' — the API "
            "fills the configured default if omitted."
        ),
    )
    monitoring_paused: bool = False


class TaskCreate(TaskBase):
    family_id: int
    created_by_person_id: Optional[int] = Field(
        default=None,
        description=(
            "Person asking to create the task. NULL when Avi can't "
            "identify the speaker (e.g. anonymous live-page caller)."
        ),
    )
    follower_person_ids: List[int] = Field(
        default_factory=list,
        description="People to attach as followers on creation.",
    )


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    assigned_to_person_id: Optional[int] = None
    start_date: Optional[date] = None
    desired_end_date: Optional[date] = None
    end_date: Optional[date] = None
    owner_kind: Optional[TaskOwnerKind] = None
    task_kind: Optional[TaskKind] = None
    cron_schedule: Optional[str] = Field(default=None, max_length=120)
    monitoring_paused: Optional[bool] = None


class TaskRead(OrmModel):
    task_id: int
    family_id: int
    created_by_person_id: Optional[int]
    assigned_to_person_id: Optional[int]
    title: str
    description: Optional[str]
    status: TaskStatus
    priority: TaskPriority
    start_date: Optional[date]
    desired_end_date: Optional[date]
    end_date: Optional[date]
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    # ------ Owner / monitoring fields ------
    owner_kind: TaskOwnerKind = "human"
    task_kind: TaskKind = "todo"
    cron_schedule: Optional[str] = None
    cron_description: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable rendering of cron_schedule (e.g. 'At 09:00 "
            "AM, every day'), produced by the `cron-descriptor` library "
            "in the API layer. Always None on non-monitoring tasks."
        ),
    )
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[TaskLastRunStatus] = None
    last_run_error: Optional[str] = None
    monitoring_paused: bool = False
    # Counts so list views can render "3 comments · 1 attachment" without
    # an N+1 round trip. The router populates these when serialising.
    follower_count: int = 0
    comment_count: int = 0
    attachment_count: int = 0
    link_count: int = 0


class TaskDetail(TaskRead):
    """Full task with its child collections inlined."""

    followers: List[TaskFollowerRead] = []
    comments: List[TaskCommentRead] = []
    attachments: List[TaskAttachmentRead] = []
    links: List[TaskLinkRead] = []


class TaskScheduleUpdate(BaseModel):
    """Payload for the dedicated schedule-edit endpoint.

    Kept separate from :class:`TaskUpdate` so the UI can PATCH
    ``cron_schedule`` and have the API atomically recompute
    ``next_run_at`` (and validate the cron string) without the caller
    having to know the cron-helpers contract.
    """

    cron_schedule: Optional[str] = Field(
        default=None,
        max_length=120,
        description=(
            "New cron expression. Validated server-side; an invalid "
            "string returns 422. Setting to NULL pauses auto-runs by "
            "clearing next_run_at."
        ),
    )
    monitoring_paused: Optional[bool] = None
