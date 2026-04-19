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
    # Counts so list views can render "3 comments · 1 attachment" without
    # an N+1 round trip. The router populates these when serialising.
    follower_count: int = 0
    comment_count: int = 0
    attachment_count: int = 0


class TaskDetail(TaskRead):
    """Full task with its child collections inlined."""

    followers: List[TaskFollowerRead] = []
    comments: List[TaskCommentRead] = []
    attachments: List[TaskAttachmentRead] = []
