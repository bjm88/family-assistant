"""The ``tasks`` family — household to-do tracking driven by Avi or by hand.

A *task* here is a piece of household work the family wants to track:
"fix the gate latch", "renew Maddie's passport", "research a summer
camp for Lily". Tasks are created by a person (the asker), can be
assigned to one owner, watched by a list of followers, accumulate
comments (from people OR from Avi as auto-notes), and can carry
attachments (PDFs, photos, scanned receipts).

Why a separate concept from :class:`AgentTask`?
------------------------------------------------
``agent_tasks`` is the audit trail of one agent loop invocation (one
chat turn that used tools). It's machine-facing and short-lived.
``tasks`` is the user-facing project board: long-lived, kanban-able,
collaboratively edited. They never refer to each other directly so
naming collision is acceptable and the user-facing word "task" stays
intuitive.

Status / priority enums
-----------------------
Stored as plain ``String`` columns guarded by ``CheckConstraint`` so
new values can be added later without an ``ALTER TYPE``. The exact
sets are exposed as module-level tuples so the API + LLM tool layer
share one source of truth with the DB.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Kanban columns, left → right. New tasks land in 'new'; the user
# drags them across the board as work progresses. 'finalizing' is the
# "review / wrap up" lane between active work and the done column.
TASK_STATUSES = ("new", "in_progress", "finalizing", "done")

# Priority ladder. 'future_idea' is intentionally LAST and means "park
# it" — it's how a casual mention ("we should think about a koi pond
# someday") gets captured without polluting the active board.
TASK_PRIORITIES = ("urgent", "high", "normal", "low", "future_idea")

# Comment authorship. The person column is nullable so an
# assistant-authored note (status changes, summaries Avi writes when
# closing a task) survives the original speaker being deleted.
TASK_COMMENT_AUTHOR_KINDS = ("person", "assistant")

# Coarse attachment categorisation. Free-form mime_type carries the
# precise media type — this is just the "show as a thumbnail vs a
# document chip" hint the UI needs.
TASK_ATTACHMENT_KINDS = ("photo", "pdf", "document", "other")

# Who is accountable for moving a task forward. Most tasks are owned
# by a person on the household ("human_task"); standing research /
# monitoring jobs are owned by the assistant itself ("ai_task") which
# is what flips a task into the cron-driven background pipeline.
TASK_OWNER_KINDS = ("human", "ai")

# Coarse shape of the task. "todo" is the classic kanban card; nothing
# changes about how it behaves. "monitoring" is an AI-owned standing
# job: a cron schedule, an LLM-driven research loop that posts its
# findings as comments + links, and a separate UI tab so it doesn't
# clutter the kanban board.
TASK_KINDS = ("todo", "monitoring")

# Outcome of the most recent monitoring run. Used by the UI to draw
# the green/red pill on the row and by the scheduler to decide whether
# to retry on a tighter cadence (not implemented today, but the column
# is shaped for it).
TASK_LAST_RUN_STATUSES = ("ok", "error", "running")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN {TASK_STATUSES!r}", name="ck_tasks_status"
        ),
        CheckConstraint(
            f"priority IN {TASK_PRIORITIES!r}", name="ck_tasks_priority"
        ),
        CheckConstraint(
            f"owner_kind IN {TASK_OWNER_KINDS!r}", name="ck_tasks_owner_kind"
        ),
        CheckConstraint(
            f"task_kind IN {TASK_KINDS!r}", name="ck_tasks_task_kind"
        ),
        CheckConstraint(
            f"last_run_status IS NULL OR last_run_status IN {TASK_LAST_RUN_STATUSES!r}",
            name="ck_tasks_last_run_status",
        ),
        # AI-owned monitoring tasks are the only kind that actually
        # need the cron + run-state columns to be populated; human todo
        # tasks leave them NULL. We *don't* hard-enforce the
        # owner_kind/task_kind/cron triad as a check constraint because
        # it makes mid-flight schema migrations (e.g. someone changes a
        # human todo into a monitoring job) painful — the validation
        # lives in the API layer instead.
        # The kanban board groups by status and filters by priority +
        # assignee — index that exact triple so the page loads in one
        # cheap scan even when the family has hundreds of tasks.
        Index(
            "ix_tasks_family_status_priority",
            "family_id",
            "status",
            "priority",
        ),
        Index(
            "ix_tasks_family_assigned",
            "family_id",
            "assigned_to_person_id",
        ),
        # The monitoring scheduler scans for "ai-owned monitoring tasks
        # whose next_run_at is due and that aren't paused" on every
        # tick. Index the exact filter so the scan stays O(due-rows)
        # even with thousands of human todos in the same table.
        Index(
            "ix_tasks_monitoring_due",
            "owner_kind",
            "task_kind",
            "monitoring_paused",
            "next_run_at",
        ),
        {
            "comment": (
                "Family-wide to-do tracker. One row per task; comments, "
                "followers, attachments and AI-discovered links live in "
                "their own tables. Two coarse shapes share this table: "
                "human-owned 'todo' kanban cards (the default), and "
                "AI-owned 'monitoring' standing research jobs that run "
                "on a cron schedule and post findings as comments + "
                "task_links. Distinct from agent_tasks (which audits a "
                "single AI loop invocation)."
            )
        },
    )

    task_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Household this task belongs to.",
    )

    # SET NULL on these so a deleted person doesn't drag their tasks
    # down with them — the task simply becomes "creator unknown" /
    # "unassigned" and stays visible on the board for the rest of the
    # household to triage.
    created_by_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Person who first asked for / created this task. NULL when "
            "Avi created the task on behalf of an unidentified speaker "
            "or when the original creator has been removed from the "
            "household."
        ),
    )
    assigned_to_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Single owner accountable for moving the task through the "
            "kanban. Optional: tasks without an owner show up under "
            "'Unassigned' and the household can adopt them."
        ),
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment='Short headline shown on the kanban card, e.g. "Renew Maddie\'s passport".',
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Long-form detail / acceptance criteria / context. The AI "
            "uses this when summarising the task back to the user."
        ),
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="new",
        server_default="new",
        comment=(
            "Kanban column. One of: new, in_progress, finalizing, done. "
            "Set completed_at when transitioning into done so reporting "
            "can answer 'what closed this week?'."
        ),
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="normal",
        server_default="normal",
        comment=(
            "One of: urgent, high, normal, low, future_idea. Avi answers "
            "'what's urgent for me?' by filtering on this column."
        ),
    )

    start_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, comment="When work on the task is intended to begin."
    )
    desired_end_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "Soft target the user wants the task wrapped up by. Distinct "
            "from end_date — desired_end_date is the WISH, end_date is "
            "the actual close. Avi uses this to surface 'due soon' work."
        ),
    )
    end_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment=(
            "Hard deadline OR actual completion date once the task is "
            "done — whichever is more useful. Set explicitly by the "
            "user; not auto-derived from completed_at."
        ),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Wall-clock timestamp the task moved into status='done'. "
            "Set automatically by the API when status transitions; "
            "cleared if status moves back out of done."
        ),
    )

    # ------ Owner / shape ----------------------------------------------
    owner_kind: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="human",
        server_default="human",
        comment=(
            "Who is accountable for moving this task forward. "
            "'human' = a household member (assigned_to_person_id); "
            "'ai' = the assistant itself, which only makes sense in "
            "combination with task_kind='monitoring'. Defaults to "
            "'human' so existing rows behave exactly as before."
        ),
    )
    task_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="todo",
        server_default="todo",
        comment=(
            "Coarse shape of the task. 'todo' = a kanban card on the "
            "main board. 'monitoring' = an AI-owned standing research "
            "job (cron-driven, posts findings as comments + links, "
            "lives on its own UI tab). The kanban view filters to "
            "task_kind='todo' so monitoring jobs don't pollute the board."
        ),
    )

    # ------ Cron / schedule (only meaningful for AI monitoring tasks) --
    cron_schedule: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment=(
            "Standard 5-field cron expression "
            "(minute hour day-of-month month day-of-week) describing "
            "when the monitoring job should run. Interpreted in the "
            "owning family's timezone (families.timezone). NULL on "
            "human todo tasks; required on AI monitoring tasks (the "
            "API fills the default '0 9 * * *' if the creator omits it)."
        ),
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Wall-clock UTC of the next scheduled monitoring run. "
            "Computed from cron_schedule + family timezone whenever the "
            "schedule changes or a run completes. The scheduler scans "
            "this column to find due work — NULL means 'never auto-run' "
            "(use 'Run now' to fire manually)."
        ),
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Wall-clock UTC of the most recent monitoring run start.",
    )
    last_run_status: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        comment=(
            "Outcome of the last monitoring run: 'ok' (agent completed "
            "and posted a comment), 'error' (the run raised), 'running' "
            "(currently executing — protects against the scheduler "
            "double-firing the same task). NULL until the first run."
        ),
    )
    last_run_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Short error message captured when last_run_status='error'. "
            "Surfaced in the monitoring tab so the user can tell at a "
            "glance whether a job has been silently failing."
        ),
    )
    monitoring_paused: Mapped[bool] = mapped_column(
        Boolean,
        # Defaults to FALSE so a freshly created monitoring task starts
        # running immediately on its schedule. Toggle via the UI to
        # take a job offline without losing the cron / context.
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "When true, the scheduler skips this task on every tick "
            "regardless of next_run_at. 'Run now' still works. "
            "Always false on human todo tasks."
        ),
    )

    family: Mapped["Family"] = relationship(back_populates="tasks")  # noqa: F821
    created_by: Mapped[Optional["Person"]] = relationship(  # noqa: F821
        foreign_keys=[created_by_person_id],
    )
    assigned_to: Mapped[Optional["Person"]] = relationship(  # noqa: F821
        foreign_keys=[assigned_to_person_id],
    )
    followers: Mapped[List["TaskFollower"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    comments: Mapped[List["TaskComment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskComment.created_at.asc()",
    )
    attachments: Mapped[List["TaskAttachment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskAttachment.created_at.asc()",
    )
    links: Mapped[List["TaskLink"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskLink.created_at.desc()",
    )


class TaskFollower(Base):
    __tablename__ = "task_followers"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "person_id", name="uq_task_followers_task_person"
        ),
        Index("ix_task_followers_person", "person_id"),
        {
            "comment": (
                "Who is watching a task — copied on comments / status "
                "changes when notification routing is wired up. The "
                "assignee + creator are followers implicitly; this "
                "table records the EXTRA people Avi or a user has "
                "looped in."
            )
        },
    )

    task_follower_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the follower was attached to the task.",
    )

    task: Mapped["Task"] = relationship(back_populates="followers")


class TaskComment(Base):
    __tablename__ = "task_comments"
    __table_args__ = (
        CheckConstraint(
            f"author_kind IN {TASK_COMMENT_AUTHOR_KINDS!r}",
            name="ck_task_comments_author_kind",
        ),
        Index("ix_task_comments_task_created", "task_id", "created_at"),
        {
            "comment": (
                "Append-only conversation thread on a task. Comments "
                "are written by household members (author_kind='person') "
                "OR by Avi as auto-notes (author_kind='assistant', "
                "author_person_id=NULL) — e.g. when Avi marks a task "
                "done and records a one-line summary."
            )
        },
    )

    task_comment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Author when author_kind='person'. NULL for "
            "assistant-authored comments and for person-authored "
            "comments whose author was later deleted."
        ),
    )
    author_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="person",
        server_default="person",
    )

    body: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Plain-text comment body."
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the comment was recorded.",
    )

    task: Mapped["Task"] = relationship(back_populates="comments")


class TaskAttachment(Base):
    __tablename__ = "task_attachments"
    __table_args__ = (
        CheckConstraint(
            f"attachment_kind IN {TASK_ATTACHMENT_KINDS!r}",
            name="ck_task_attachments_kind",
        ),
        Index("ix_task_attachments_task", "task_id"),
        {
            "comment": (
                "Files attached to a task — receipts, photos of the "
                "broken part, the PDF brochure for the summer camp. "
                "Bytes live on the local filesystem under "
                "FA_STORAGE_ROOT/family_<id>/tasks/task_<id>/; this "
                "row is the metadata."
            )
        },
    )

    task_attachment_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    uploaded_by_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
    )

    attachment_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="document",
        server_default="document",
        comment=(
            "Coarse category for UI rendering: photo (image thumbnail), "
            "pdf (PDF chip with preview link), document (generic file), "
            "other. The exact format lives in mime_type."
        ),
    )

    stored_file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Relative path under FA_STORAGE_ROOT.",
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    caption: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional short description shown next to the file.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the file was uploaded.",
    )

    task: Mapped["Task"] = relationship(back_populates="attachments")


class TaskLink(Base):
    """A URL the AI surfaced while researching a monitoring task.

    Distinct from :class:`TaskAttachment` (which carries on-disk bytes).
    Links live in their own table so the monitoring tab can render
    "Avi found these 12 sources this week" cleanly without us
    overloading attachment_kind with sentinels like 'link'. Anyone can
    add a link via the UI; the AI adds them through the
    ``task_attach_link`` tool when it cites a source.
    """

    __tablename__ = "task_links"
    __table_args__ = (
        Index("ix_task_links_task_created", "task_id", "created_at"),
        # Same URL surfaced twice on the same task is almost always
        # noise — the model will sometimes re-cite a source it already
        # added on a previous run. Unique-on-(task_id, url) collapses
        # the dup at write time so the UI doesn't have to dedupe.
        UniqueConstraint("task_id", "url", name="uq_task_links_task_url"),
        {
            "comment": (
                "External URLs the assistant cited while working on a "
                "task — typically populated by the monitoring agent "
                "loop as it researches with the web_search tool. The "
                "UI renders these as a flat list of source chips on "
                "the task detail page."
            )
        },
    )

    task_link_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
        comment="Full URL the assistant (or a person) wants to cite.",
    )
    title: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Display label for the link — typically the page <title> "
            "captured by the web_search result. Falls back to the URL "
            "host when missing."
        ),
    )
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "One-paragraph summary of why this link is relevant. "
            "Populated by the monitoring agent so the user doesn't "
            "have to click through every source."
        ),
    )
    added_by_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="assistant",
        server_default="assistant",
        comment=(
            "'assistant' (added by Avi during a monitoring run) or "
            "'person' (manually added in the UI)."
        ),
    )
    added_by_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment="Author when added_by_kind='person'.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the link was attached to the task.",
    )

    task: Mapped["Task"] = relationship(back_populates="links")
