"""Add the family ``tasks`` board (kanban) + comments / followers / attachments.

Four new tables:

* ``tasks``                — one row per household to-do.
* ``task_followers``       — junction (people watching a task).
* ``task_comments``        — append-only thread (people OR Avi).
* ``task_attachments``     — files (photos, PDFs, generic docs) on disk.

The data model intentionally mirrors a small Trello / Linear board so the
UI can render a kanban grouped by ``status`` and filtered by
``priority`` + ``assigned_to_person_id``. The AI assistant (Avi) reads /
writes via the same tables so "track that as a task" and "what's
urgent for me?" stay one query away.

Distinct from the existing ``agent_tasks`` table, which audits a single
AI agent loop invocation. They never reference each other.

Revision ID: 0018_tasks
Revises: 0017_person_work_email
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0018_tasks"
down_revision: Union[str, None] = "0017_person_work_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STATUSES = "('new', 'in_progress', 'finalizing', 'done')"
_PRIORITIES = "('urgent', 'high', 'normal', 'low', 'future_idea')"
_AUTHOR_KINDS = "('person', 'assistant')"
_ATTACHMENT_KINDS = "('photo', 'pdf', 'document', 'other')"


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Person who first asked for / created this task. NULL "
                "when Avi created the task on behalf of an unidentified "
                "speaker, or when the original creator was removed."
            ),
        ),
        sa.Column(
            "assigned_to_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Single owner accountable for the task. Optional — "
                "unassigned tasks render under 'Unassigned' on the board."
            ),
        ),
        sa.Column(
            "title",
            sa.String(200),
            nullable=False,
            comment="Short headline shown on the kanban card.",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment=(
                "Long-form detail / acceptance criteria / context. The "
                "AI uses this when summarising the task back to the user."
            ),
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="new",
            comment=(
                "Kanban column. One of: new, in_progress, finalizing, "
                "done. completed_at is set when transitioning to done."
            ),
        ),
        sa.Column(
            "priority",
            sa.String(20),
            nullable=False,
            server_default="normal",
            comment=(
                "One of: urgent, high, normal, low, future_idea. Avi "
                "answers 'what's urgent?' by filtering this column."
            ),
        ),
        sa.Column(
            "start_date",
            sa.Date(),
            nullable=True,
            comment="When work on the task is intended to begin.",
        ),
        sa.Column(
            "desired_end_date",
            sa.Date(),
            nullable=True,
            comment=(
                "Soft target the user wants the task wrapped up by. "
                "Distinct from end_date — desired is a wish, end is the "
                "actual close."
            ),
        ),
        sa.Column(
            "end_date",
            sa.Date(),
            nullable=True,
            comment=(
                "Hard deadline OR actual completion date once done. "
                "Set explicitly by the user; not auto-derived."
            ),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Wall-clock time the task moved into status='done'. "
                "Cleared automatically if status moves back out of done."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_tasks_status"),
        sa.CheckConstraint(
            f"priority IN {_PRIORITIES}", name="ck_tasks_priority"
        ),
        comment=(
            "Family-wide kanban to-do tracker. Distinct from agent_tasks "
            "(which audits one AI loop invocation) — this is the human-"
            "facing task board."
        ),
    )
    op.create_index("ix_tasks_family_id", "tasks", ["family_id"])
    op.create_index(
        "ix_tasks_family_status_priority",
        "tasks",
        ["family_id", "status", "priority"],
    )
    op.create_index(
        "ix_tasks_family_assigned",
        "tasks",
        ["family_id", "assigned_to_person_id"],
    )
    op.create_index(
        "ix_tasks_created_by_person_id", "tasks", ["created_by_person_id"]
    )
    op.create_index(
        "ix_tasks_assigned_to_person_id", "tasks", ["assigned_to_person_id"]
    )

    op.create_table(
        "task_followers",
        sa.Column(
            "task_follower_id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "task_id", "person_id", name="uq_task_followers_task_person"
        ),
        comment=(
            "Who is watching a task — extra people Avi or a user has "
            "looped in beyond the implicit creator + assignee."
        ),
    )
    op.create_index("ix_task_followers_task_id", "task_followers", ["task_id"])
    op.create_index(
        "ix_task_followers_person", "task_followers", ["person_id"]
    )

    op.create_table(
        "task_comments",
        sa.Column(
            "task_comment_id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Author when author_kind='person'. NULL for "
                "assistant-authored comments and for person-authored "
                "comments whose author was later deleted."
            ),
        ),
        sa.Column(
            "author_kind",
            sa.String(20),
            nullable=False,
            server_default="person",
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"author_kind IN {_AUTHOR_KINDS}",
            name="ck_task_comments_author_kind",
        ),
        comment=(
            "Append-only conversation thread on a task. Comments come "
            "from household members or from Avi as auto-notes."
        ),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_index(
        "ix_task_comments_task_created",
        "task_comments",
        ["task_id", "created_at"],
    )

    op.create_table(
        "task_attachments",
        sa.Column(
            "task_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "attachment_kind",
            sa.String(20),
            nullable=False,
            server_default="document",
        ),
        sa.Column("stored_file_path", sa.String(500), nullable=False),
        sa.Column("original_file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"attachment_kind IN {_ATTACHMENT_KINDS}",
            name="ck_task_attachments_kind",
        ),
        comment=(
            "Files attached to a task — receipts, photos of the broken "
            "part, the PDF brochure for the summer camp."
        ),
    )
    op.create_index(
        "ix_task_attachments_task", "task_attachments", ["task_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_attachments_task", table_name="task_attachments"
    )
    op.drop_table("task_attachments")

    op.drop_index("ix_task_comments_task_created", table_name="task_comments")
    op.drop_index("ix_task_comments_task_id", table_name="task_comments")
    op.drop_table("task_comments")

    op.drop_index("ix_task_followers_person", table_name="task_followers")
    op.drop_index("ix_task_followers_task_id", table_name="task_followers")
    op.drop_table("task_followers")

    op.drop_index("ix_tasks_assigned_to_person_id", table_name="tasks")
    op.drop_index("ix_tasks_created_by_person_id", table_name="tasks")
    op.drop_index("ix_tasks_family_assigned", table_name="tasks")
    op.drop_index("ix_tasks_family_status_priority", table_name="tasks")
    op.drop_index("ix_tasks_family_id", table_name="tasks")
    op.drop_table("tasks")
