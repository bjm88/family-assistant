"""``agent_tasks`` + ``agent_steps`` — audit + status for the AI agent loop.

Every chat turn that is dispatched through the agent (i.e. has tools
available) creates an :class:`AgentTask` row. As the agent runs we
append :class:`AgentStep` rows for each model thought, tool call, and
tool result. The combination gives:

* a complete, replayable transcript for debugging ("why did Avi pick
  ``sql_query`` instead of ``lookup_person``?")
* a stable id the UI can subscribe to via SSE for live updates,
* a foundation for genuinely background jobs ("research X and email
  me a summary in 20 minutes") — the same row keeps growing as the
  worker runs.

Status state machine
--------------------
::

    pending → running → succeeded
                    └ → failed
                    └ → cancelled

The ``running`` row is the one the SSE channel is streaming to. The
worker writes terminal status + ``completed_at`` exactly once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


# Statuses are stored as plain text rather than a Postgres ENUM so we
# can add more (``awaiting_input``, ``deferred``, …) later without an
# ALTER TYPE migration; the CHECK constraint enforces the current set.
_AGENT_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
_AGENT_STEP_TYPES = (
    "thinking",   # model produced reasoning text but no tool call
    "tool_call",  # model requested a tool execution
    "tool_result",  # we executed it and recorded what came back
    "final",      # final user-facing answer streamed at end of run
    "error",      # uncaught failure inside the loop
)


class AgentTask(Base, TimestampMixin):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN {_AGENT_STATUSES!r}",
            name="ck_agent_tasks_status",
        ),
        Index("ix_agent_tasks_family_created", "family_id", "created_at"),
        Index("ix_agent_tasks_session", "live_session_id"),
        {
            "comment": (
                "One row per agent invocation (every chat turn that has "
                "tools available). Holds the user prompt, terminal status, "
                "final assistant reply, and timing — see agent_steps for "
                "the per-step transcript."
            )
        },
    )

    agent_task_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
    )
    live_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Session the chat turn belonged to, when the request came "
            "from the live AI page. NULL for ad-hoc API calls."
        ),
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment="Recognised speaker, when known.",
    )

    kind: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="chat",
        server_default="chat",
        comment=(
            "Coarse category — 'chat' for normal turns, 'research' / "
            "'background' for longer multi-step jobs scheduled via "
            "/api/aiassistant/tasks."
        ),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )

    input_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The user message that triggered this task.",
    )
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Final natural-language assistant reply on success.",
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Terminal error message when status='failed'.",
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="Wall-clock runtime, populated on completion."
    )

    model: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment="Primary LLM tag the loop drove (e.g. gemma4:26b).",
    )

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="AgentStep.step_index",
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        CheckConstraint(
            f"step_type IN {_AGENT_STEP_TYPES!r}",
            name="ck_agent_steps_type",
        ),
        Index("ix_agent_steps_task_index", "agent_task_id", "step_index"),
        {
            "comment": (
                "Append-only transcript of an agent task's plan/execute "
                "/observe loop. Each row is one model emission or tool "
                "execution; replaying them in order reproduces the run."
            )
        },
    )

    agent_step_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    agent_task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_tasks.agent_task_id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonic per-task index, 0-based.",
    )
    step_type: Mapped[str] = mapped_column(String(20), nullable=False)

    tool_name: Mapped[Optional[str]] = mapped_column(
        String(80),
        nullable=True,
        comment="Populated for step_type='tool_call' and 'tool_result'.",
    )
    tool_input: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )
    tool_output: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    content: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form text — the model's prose for 'thinking', the "
            "human-readable summary for 'tool_result' / 'final', or the "
            "exception detail for 'error'."
        ),
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Wall-clock time the step was recorded.",
    )

    task: Mapped["AgentTask"] = relationship(back_populates="steps")
