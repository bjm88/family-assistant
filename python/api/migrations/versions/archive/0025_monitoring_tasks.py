"""Owner / kind / cron schedule on tasks + task_links table + family timezone.

Adds the schema needed for **AI-owned monitoring tasks** — Avi's
standing research jobs. Three changes:

1. ``families.timezone`` (text, default ``America/New_York``) — IANA
   timezone name used to interpret per-family cron schedules. Without
   this every "9am daily" job would mean different things to different
   households on the same backend.

2. ``tasks`` gets eight new columns:
   - ``owner_kind`` ('human' | 'ai', default 'human')
   - ``task_kind`` ('todo' | 'monitoring', default 'todo')
   - ``cron_schedule`` (nullable text)
   - ``next_run_at`` (nullable timestamptz) — when the scheduler should
     fire next
   - ``last_run_at`` (nullable timestamptz)
   - ``last_run_status`` ('ok' | 'error' | 'running', nullable)
   - ``last_run_error`` (nullable text)
   - ``monitoring_paused`` (boolean, default false)

   Plus three CHECK constraints on the enum columns and a
   ``ix_tasks_monitoring_due`` index that makes the scheduler's
   "find due ai-monitoring tasks" scan O(due-rows).

   All new columns have safe defaults so existing tasks continue to
   behave exactly as human todos.

3. ``task_links`` (new table) — external URLs the assistant cites
   while researching a monitoring task. Distinct from
   ``task_attachments`` (which carries on-disk bytes).
   Unique-on-(task_id, url) so a re-run that surfaces the same source
   twice is collapsed at write time.

Why one revision instead of three
---------------------------------
All three changes are required to make a monitoring task run
end-to-end; splitting them would leave the schema in a state where
the API rejects the new task shape until two more migrations land.
Keeping them together means a single ``alembic upgrade head`` flips
the feature on or off cleanly.

Revision ID: 0025_monitoring_tasks
Revises: 0024_person_ai_calendar_write
Create Date: 2026-04-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0025_monitoring_tasks"
down_revision: Union[str, None] = "0024_person_ai_calendar_write"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Re-declared here rather than imported from python.api.models.task so
# the migration is a self-contained unit and can be re-run on a stripped
# checkout without importing the full app package.
_TASK_OWNER_KINDS = ("human", "ai")
_TASK_KINDS = ("todo", "monitoring")
_TASK_LAST_RUN_STATUSES = ("ok", "error", "running")


def upgrade() -> None:
    # 1. families.timezone
    op.add_column(
        "families",
        sa.Column(
            "timezone",
            sa.String(64),
            nullable=False,
            server_default="America/New_York",
            comment=(
                "IANA timezone name (e.g. 'America/New_York', "
                "'Europe/London') used to interpret cron schedules on "
                "this family's monitoring tasks and to format "
                "wall-clock times in the UI. Defaults to "
                "America/New_York; change via the family-settings page."
            ),
        ),
    )

    # 2. tasks columns
    op.add_column(
        "tasks",
        sa.Column(
            "owner_kind",
            sa.String(10),
            nullable=False,
            server_default="human",
            comment=(
                "Who is accountable for moving this task forward. "
                "'human' = a household member (assigned_to_person_id); "
                "'ai' = the assistant itself, which only makes sense in "
                "combination with task_kind='monitoring'. Defaults to "
                "'human' so existing rows behave exactly as before."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "task_kind",
            sa.String(20),
            nullable=False,
            server_default="todo",
            comment=(
                "Coarse shape of the task. 'todo' = a kanban card on the "
                "main board. 'monitoring' = an AI-owned standing research "
                "job (cron-driven, posts findings as comments + links, "
                "lives on its own UI tab). The kanban view filters to "
                "task_kind='todo' so monitoring jobs don't pollute the "
                "board."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "cron_schedule",
            sa.String(120),
            nullable=True,
            comment=(
                "Standard 5-field cron expression "
                "(minute hour day-of-month month day-of-week) describing "
                "when the monitoring job should run. Interpreted in the "
                "owning family's timezone (families.timezone). NULL on "
                "human todo tasks; required on AI monitoring tasks (the "
                "API fills the default '0 9 * * *' if the creator omits "
                "it)."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Wall-clock UTC of the next scheduled monitoring run. "
                "Computed from cron_schedule + family timezone whenever "
                "the schedule changes or a run completes. The scheduler "
                "scans this column to find due work — NULL means 'never "
                "auto-run'."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Wall-clock UTC of the most recent monitoring run start.",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "last_run_status",
            sa.String(10),
            nullable=True,
            comment=(
                "Outcome of the last monitoring run: 'ok', 'error', or "
                "'running' (currently executing — protects the scheduler "
                "from double-firing the same task). NULL until first run."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "last_run_error",
            sa.Text(),
            nullable=True,
            comment=(
                "Short error message captured when "
                "last_run_status='error'."
            ),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "monitoring_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "When true, the scheduler skips this task on every tick "
                "regardless of next_run_at. 'Run now' still works."
            ),
        ),
    )

    # CHECK constraints. We use raw SQL strings so the tuple literals
    # render exactly the same way the model file does (matching IN-list
    # formatting helps grep across the codebase).
    op.create_check_constraint(
        "ck_tasks_owner_kind",
        "tasks",
        f"owner_kind IN {_TASK_OWNER_KINDS!r}",
    )
    op.create_check_constraint(
        "ck_tasks_task_kind",
        "tasks",
        f"task_kind IN {_TASK_KINDS!r}",
    )
    op.create_check_constraint(
        "ck_tasks_last_run_status",
        "tasks",
        f"last_run_status IS NULL OR last_run_status IN {_TASK_LAST_RUN_STATUSES!r}",
    )

    # Composite index used by the scheduler tick to find due monitoring
    # work in one cheap scan even with thousands of human todos around.
    op.create_index(
        "ix_tasks_monitoring_due",
        "tasks",
        ["owner_kind", "task_kind", "monitoring_paused", "next_run_at"],
    )

    # 3. task_links
    op.create_table(
        "task_links",
        sa.Column(
            "task_link_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "url",
            sa.String(2000),
            nullable=False,
            comment="Full URL the assistant (or a person) wants to cite.",
        ),
        sa.Column(
            "title",
            sa.String(500),
            nullable=True,
            comment=(
                "Display label for the link — typically the page <title> "
                "captured by the web_search result. Falls back to the "
                "URL host when missing."
            ),
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment=(
                "One-paragraph summary of why this link is relevant."
            ),
        ),
        sa.Column(
            "added_by_kind",
            sa.String(20),
            nullable=False,
            server_default="assistant",
            comment="'assistant' or 'person'.",
        ),
        sa.Column(
            "added_by_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "task_id", "url", name="uq_task_links_task_url"
        ),
        comment=(
            "External URLs the assistant cited while working on a task — "
            "typically populated by the monitoring agent loop as it "
            "researches with the web_search tool. The UI renders these "
            "as a flat list of source chips on the task detail page."
        ),
    )
    op.create_index(
        "ix_task_links_task_created", "task_links", ["task_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_links_task_created", table_name="task_links")
    op.drop_table("task_links")

    op.drop_index("ix_tasks_monitoring_due", table_name="tasks")
    op.drop_constraint("ck_tasks_last_run_status", "tasks", type_="check")
    op.drop_constraint("ck_tasks_task_kind", "tasks", type_="check")
    op.drop_constraint("ck_tasks_owner_kind", "tasks", type_="check")
    op.drop_column("tasks", "monitoring_paused")
    op.drop_column("tasks", "last_run_error")
    op.drop_column("tasks", "last_run_status")
    op.drop_column("tasks", "last_run_at")
    op.drop_column("tasks", "next_run_at")
    op.drop_column("tasks", "cron_schedule")
    op.drop_column("tasks", "task_kind")
    op.drop_column("tasks", "owner_kind")

    op.drop_column("families", "timezone")
