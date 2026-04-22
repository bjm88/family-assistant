"""Add ``agent_tasks`` + ``agent_steps`` for the AI agent loop audit trail.

Each chat turn that the agent dispatches becomes one ``agent_tasks``
row plus N ``agent_steps`` rows (one per thought, tool call, tool
result). Together they give a fully replayable transcript and a stable
id the live UI can subscribe to via SSE.

Revision ID: 0015_agent_tasks
Revises: 0014_google_oauth_credentials
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0015_agent_tasks"
down_revision: Union[str, None] = "0014_google_oauth_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STATUSES = "('pending', 'running', 'succeeded', 'failed', 'cancelled')"
_STEP_TYPES = "('thinking', 'tool_call', 'tool_result', 'final', 'error')"


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("agent_task_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "live_session_id",
            sa.Integer(),
            sa.ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Session the chat turn belonged to, when the request came "
                "from the live AI page."
            ),
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment="Recognised speaker, when known.",
        ),
        sa.Column(
            "kind",
            sa.String(40),
            nullable=False,
            server_default="chat",
            comment=(
                "Coarse category — 'chat' for normal turns, 'research' / "
                "'background' for longer multi-step jobs."
            ),
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "input_text",
            sa.Text(),
            nullable=False,
            comment="The user message that triggered this task.",
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="Final natural-language assistant reply on success.",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="Terminal error message when status='failed'.",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "model",
            sa.String(120),
            nullable=True,
            comment="Primary LLM tag the loop drove (e.g. gemma4:26b).",
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
        sa.CheckConstraint(
            f"status IN {_STATUSES}",
            name="ck_agent_tasks_status",
        ),
        comment=(
            "One row per agent invocation. Holds the user prompt, "
            "terminal status, final assistant reply, and timing."
        ),
    )
    op.create_index(
        "ix_agent_tasks_family_created",
        "agent_tasks",
        ["family_id", "created_at"],
    )
    op.create_index(
        "ix_agent_tasks_session", "agent_tasks", ["live_session_id"]
    )

    op.create_table(
        "agent_steps",
        sa.Column("agent_step_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_task_id",
            sa.Integer(),
            sa.ForeignKey("agent_tasks.agent_task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_index",
            sa.Integer(),
            nullable=False,
            comment="Monotonic per-task index, 0-based.",
        ),
        sa.Column("step_type", sa.String(20), nullable=False),
        sa.Column(
            "tool_name",
            sa.String(80),
            nullable=True,
            comment="Populated for step_type='tool_call' and 'tool_result'.",
        ),
        sa.Column("tool_input", JSONB(), nullable=True),
        sa.Column("tool_output", JSONB(), nullable=True),
        sa.Column(
            "content",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form text — the model's prose for 'thinking', the "
                "human summary for 'tool_result' / 'final', or the "
                "exception detail for 'error'."
            ),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"step_type IN {_STEP_TYPES}",
            name="ck_agent_steps_type",
        ),
        comment=(
            "Append-only transcript of an agent task's plan/execute/"
            "observe loop."
        ),
    )
    op.create_index(
        "ix_agent_steps_task_index",
        "agent_steps",
        ["agent_task_id", "step_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_steps_task_index", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_tasks_session", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_family_created", table_name="agent_tasks")
    op.drop_table("agent_tasks")
