"""Scheduler for AI-owned monitoring tasks (Avi's standing research jobs).

A monitoring task is a row in ``tasks`` with ``owner_kind='ai'`` and
``task_kind='monitoring'``. It carries a cron schedule (interpreted in
its family's timezone) and Avi runs it on cadence, posting findings as
``task_comments`` and citing sources via ``task_links``.

Architecture mirrors the existing email / telegram inbox poller:

* :func:`run_monitoring_loop` is a long-running asyncio coroutine that
  ticks every ``AI_MONITORING_TICK_SECONDS``. It scans for due tasks
  (``next_run_at <= now()`` AND ``last_run_status != 'running'`` AND
  ``monitoring_paused = false``), atomically claims each one by
  flipping ``last_run_status='running'`` + bumping ``next_run_at`` to
  the *following* tick (so a long-running job doesn't double-fire),
  and submits the heavy work to :mod:`api.services.background_agent`.

* :func:`run_now_in_background` is the manual-fire entry point used by
  the "Run now" button + the immediate-first-run path on monitoring
  task creation. It runs the same per-task work but skips the cron-due
  check.

* The per-task worker (:func:`_run_one_monitoring_task`) opens its own
  DB session, builds a system prompt, calls :func:`agent.run_agent`
  with the dedicated thinking model + ``think=True`` flag, and writes
  the result back as a ``task_comments`` row + status update. All
  exceptions are captured and surfaced as ``last_run_status='error'``;
  nothing in the loop is allowed to propagate up and kill the
  scheduler thread.

Why a single global scheduler instead of one-per-family
-------------------------------------------------------
We expect dozens, not thousands, of monitoring tasks per backend, so a
single in-process scheduler is plenty. It also means exactly one
process owns the cron tick, which removes the "two backends both
firing the same cron" race entirely. If we ever scale out we'll move
to a database-backed lease (``SELECT … FOR UPDATE SKIP LOCKED``) — the
single-tick design here makes that swap a one-function change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..ai import agent as agent_loop
from ..ai import authz
from ..ai import ollama, prompts, rag, schema_catalog
from ..ai import tools as agent_tools
from ..config import get_settings
from ..db import SessionLocal
from . import background_agent, cron_helpers


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_monitoring_loop(stop_event: asyncio.Event) -> None:
    """Forever-running scheduler tick. Cancel by setting ``stop_event``.

    Sleeps in 1s slices so a graceful shutdown returns within a second
    no matter what the configured tick interval is.
    """
    settings = get_settings()
    if not settings.AI_MONITORING_ENABLED:
        logger.info(
            "Monitoring scheduler disabled via AI_MONITORING_ENABLED=false."
        )
        return

    interval = max(5, int(settings.AI_MONITORING_TICK_SECONDS))
    logger.info(
        "Monitoring scheduler starting (interval=%ds, default_cron=%r, "
        "thinking_model=%r, web_search=%s).",
        interval,
        settings.AI_MONITORING_DEFAULT_CRON,
        settings.AI_OLLAMA_THINKING_MODEL or settings.AI_OLLAMA_MODEL,
        settings.FA_SEARCH_PROVIDER or "(disabled)",
    )

    while not stop_event.is_set():
        tick_started = time.monotonic()
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:  # noqa: BLE001 - never let the scheduler die
            logger.exception("Monitoring scheduler tick crashed; continuing")

        elapsed = time.monotonic() - tick_started
        remaining = max(0.0, interval - elapsed)
        await _sleep_with_stop(remaining, stop_event)

    logger.info("Monitoring scheduler stopped.")


def run_now_in_background(task_id: int) -> None:
    """Submit one monitoring task to the agent pool, immediately.

    Used by:
    * the ``POST /tasks/{id}/run-now`` endpoint
    * the immediate-first-run path on monitoring-task creation
    * ``task_set_schedule`` AI tool when the user wants instant feedback

    Returns immediately — callers do not wait for the run to finish.
    """
    background_agent.submit(lambda: _run_one_monitoring_task(task_id))


# ---------------------------------------------------------------------------
# Tick + claim
# ---------------------------------------------------------------------------


async def _sleep_with_stop(seconds: float, stop_event: asyncio.Event) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(1.0, remaining))
            return
        except asyncio.TimeoutError:
            continue


def _tick_once() -> None:
    """Scan for due monitoring tasks and submit each one.

    Each due task is "claimed" inside its own short transaction:
    ``last_run_status='running'`` is set, ``last_run_at`` is stamped,
    and ``next_run_at`` is rolled forward to the cron's *following*
    fire-time. Doing this BEFORE the heavy work means a long-running
    job (or a slow Ollama call) can't possibly trigger a second
    concurrent run on the next tick — the next tick's scan filters
    out rows already in 'running'.
    """
    now = datetime.now(timezone.utc)
    with _session() as db:
        due_ids = _find_due_task_ids(db, now)
        for task_id in due_ids:
            try:
                _claim_for_run(db, task_id, now)
            except Exception:  # noqa: BLE001 - per-task isolation
                logger.exception(
                    "monitoring_scheduler: failed to claim task %d",
                    task_id,
                )
                db.rollback()
                continue
            background_agent.submit(
                lambda tid=task_id: _run_one_monitoring_task(tid)
            )


def _find_due_task_ids(db: Session, now: datetime) -> List[int]:
    """All AI monitoring tasks whose next_run_at is past + not running."""
    stmt = (
        select(models.Task.task_id)
        .where(models.Task.owner_kind == "ai")
        .where(models.Task.task_kind == "monitoring")
        .where(models.Task.monitoring_paused.is_(False))
        .where(models.Task.next_run_at.is_not(None))
        .where(models.Task.next_run_at <= now)
        .where(
            (models.Task.last_run_status.is_(None))
            | (models.Task.last_run_status != "running")
        )
        .order_by(models.Task.next_run_at.asc())
        # Cap per tick so an unexpected backlog doesn't slam Ollama.
        .limit(20)
    )
    return [row for row, in db.execute(stmt).all()]


def _claim_for_run(db: Session, task_id: int, now: datetime) -> None:
    """Mark a task as running + push next_run_at to the *next* fire."""
    task = db.get(models.Task, task_id)
    if task is None:
        return
    if task.last_run_status == "running":
        return  # raced with another tick
    task.last_run_status = "running"
    task.last_run_at = now
    task.last_run_error = None
    if task.cron_schedule:
        tz_name = _resolve_family_timezone(db, task.family_id)
        try:
            task.next_run_at = cron_helpers.next_run(
                task.cron_schedule, tz_name, after=now
            )
        except cron_helpers.CronError:
            # Stored cron drifted into invalid (shouldn't happen — the
            # API validates on write — but be defensive). Park the
            # task by clearing next_run_at so the scheduler won't try
            # again until a human fixes the cron.
            task.next_run_at = None
    db.commit()


# ---------------------------------------------------------------------------
# The actual work
# ---------------------------------------------------------------------------


def _run_one_monitoring_task(task_id: int) -> None:
    """Drive a single monitoring run end-to-end (synchronous worker).

    Always tries to leave the task row in a sensible state: 'ok' on
    success, 'error' on failure (with the message). Exceptions are
    swallowed so a crash here can't take down the scheduler thread.
    """
    started_at = datetime.now(timezone.utc)
    try:
        with _session() as db:
            task = db.get(models.Task, task_id)
            if task is None:
                logger.warning(
                    "monitoring_scheduler: task %d disappeared before run",
                    task_id,
                )
                return
            family_id = task.family_id
            title = task.title
            description = task.description
            cron_schedule = task.cron_schedule
            assistant_id = _assistant_id_for_family(db, family_id)
            speaker_person_id = task.created_by_person_id
            system_prompt = _build_monitoring_system_prompt(
                db,
                family_id=family_id,
                assistant_id=assistant_id,
                speaker_person_id=speaker_person_id,
                task=task,
            )

        # Open a fresh AgentTask audit row outside of the previous
        # session so the heavy work doesn't hold a session open.
        with _session() as db:
            agent_task = agent_loop.create_task(
                db,
                family_id=family_id,
                input_text=_build_user_message(title, description, cron_schedule),
                person_id=speaker_person_id,
                kind="monitoring",
                model=_thinking_model(),
            )
            agent_task_id = agent_task.agent_task_id
            db.commit()

        final_text = _drain_agent(
            agent_task_id=agent_task_id,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=speaker_person_id,
            system_prompt=system_prompt,
            user_message=_build_user_message(title, description, cron_schedule),
        )

        # Persist the comment + flip the run status.
        with _session() as db:
            task = db.get(models.Task, task_id)
            if task is None:
                return
            comment_body = (final_text or "").strip()
            if not comment_body:
                comment_body = (
                    "(Monitoring run finished but produced no narrative — "
                    "the agent may have hit a tool error. Check the agent "
                    "task log for details.)"
                )
            db.add(
                models.TaskComment(
                    task_id=task.task_id,
                    author_person_id=None,
                    author_kind="assistant",
                    body=comment_body,
                    created_at=datetime.now(timezone.utc),
                )
            )
            task.last_run_status = "ok"
            task.last_run_error = None
            # Stamp here too — the immediate-first-run path
            # (``run_now_in_background``) skips ``_claim_for_run``,
            # so without this both paths wouldn't write the same
            # timestamp.
            task.last_run_at = datetime.now(timezone.utc)
            db.commit()

        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        logger.info(
            "monitoring_scheduler: task %d ('%s') ran in %.1fs (agent_task=%d)",
            task_id,
            title,
            elapsed,
            agent_task_id,
        )

    except Exception as exc:  # noqa: BLE001 - last-ditch
        logger.exception(
            "monitoring_scheduler: task %d failed", task_id
        )
        try:
            with _session() as db:
                task = db.get(models.Task, task_id)
                if task is not None:
                    task.last_run_status = "error"
                    task.last_run_error = (str(exc) or type(exc).__name__)[:1000]
                    task.last_run_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "monitoring_scheduler: failed to record error for task %d",
                task_id,
            )


# ---------------------------------------------------------------------------
# Agent prompt + drain
# ---------------------------------------------------------------------------


def _drain_agent(
    *,
    agent_task_id: int,
    family_id: int,
    assistant_id: Optional[int],
    person_id: Optional[int],
    system_prompt: str,
    user_message: str,
) -> str:
    """Drive the async agent generator from a sync worker thread.

    Mirrors :func:`api.services.email_inbox._run_agent_to_completion`
    but injects the dedicated thinking model + ``think=True`` so
    monitoring runs benefit from extended reasoning.
    """
    settings = get_settings()
    registry = agent_tools.build_default_registry()
    with _session() as db:
        capabilities = agent_tools.detect_capabilities(db, assistant_id)

    model_override = _thinking_model()
    think_flag: Optional[bool] = (
        True if settings.AI_OLLAMA_THINKING_ENABLED else None
    )

    final_text = ""
    error_text: Optional[str] = None

    async def _drain() -> None:
        nonlocal final_text, error_text
        async for event in agent_loop.run_agent(
            task_id=agent_task_id,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=person_id,
            system_prompt=system_prompt,
            history=[],
            user_message=user_message,
            registry=registry,
            capabilities=capabilities,
            model_override=model_override,
            think=think_flag,
            # Monitoring runs need more steps than a chat turn —
            # search → synthesis → attach links → write comment →
            # finalize easily exceeds the 5-step chat default.
            max_steps=settings.AI_MONITORING_MAX_STEPS,
        ):
            if event.type == "task_completed":
                final_text = (event.payload.get("summary") or "").strip()
            elif event.type == "task_failed":
                error_text = str(event.payload.get("error") or "Agent failed.")

    try:
        asyncio.run(_drain())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drain())
        finally:
            loop.close()

    if error_text and not final_text:
        raise RuntimeError(error_text)
    return final_text


def _build_user_message(
    title: str, description: Optional[str], cron_schedule: Optional[str]
) -> str:
    parts = [
        f"Standing monitoring task: {title}",
    ]
    if description:
        parts.append("")
        parts.append(description.strip())
    if cron_schedule:
        parts.append("")
        parts.append(f"Schedule: {cron_schedule} ({cron_helpers.describe(cron_schedule)})")
    parts.append("")
    parts.append(
        "Please research this topic now using the tools available to you "
        "(web_search for fresh information, task_attach_link to cite "
        "sources you reference, task_add_comment if you want to break "
        "the report into sections rather than one comment). Your final "
        "narrative answer becomes the new comment posted to the task — "
        "make it useful, concise, and structured."
    )
    return "\n".join(parts)


def _build_monitoring_system_prompt(
    db: Session,
    *,
    family_id: int,
    assistant_id: Optional[int],
    speaker_person_id: Optional[int],
    task: models.Task,
) -> str:
    """System prompt used for a single monitoring run.

    Reuses the existing capability / RAG / schema scaffolding from the
    chat path so Avi sees the same world she sees on a live message,
    plus a dedicated "How to handle this monitoring run" trailer that
    shapes the tool-use cadence (search → cite → comment) and the
    final-answer format.
    """
    family = db.get(models.Family, family_id)
    assistant_name = (
        family.assistant.assistant_name if family and family.assistant else "Avi"
    )
    family_name = family.family_name if family else None

    registry = agent_tools.build_default_registry()
    capabilities = agent_tools.detect_capabilities(db, assistant_id)
    capabilities_block = agent_tools.describe_capabilities(registry, capabilities)

    parts = [
        ollama.system_prompt_for_avi(assistant_name, family_name),
        "--- What you can do ---\n" + capabilities_block,
    ]
    house_context = prompts.render_context_blocks()
    if house_context:
        parts.append("--- House context ---\n" + house_context)

    parts.append(
        authz.render_speaker_scope_block(
            authz.build_speaker_scope(db, speaker_person_id=speaker_person_id)
        )
    )

    if family is not None:
        rag_block = rag.build_family_overview(
            db, family, requestor_person_id=speaker_person_id
        )
        if rag_block:
            parts.append("--- Known household context ---\n" + rag_block)

    parts.append(
        "--- Database schema you can query ---\n"
        "You have read-only access to the family Postgres database. "
        "Use the sql tool sparingly — most monitoring tasks need WEB "
        "research, not household data.\n\n"
        + schema_catalog.dump_text(db)
    )

    parts.append(
        f"--- This monitoring run (task #{task.task_id}: {task.title!r}) ---\n"
        "You are running a SCHEDULED monitoring task that you (or a "
        "household member) created earlier. The user is not actively "
        "watching — your output becomes a single task_comment that they "
        "will read later. So:\n"
        "* Lead with what's NEW since the last run (if anything). The "
        "  task description tells you what the user cares about.\n"
        "* Use the web_search tool to gather fresh information. Run "
        "  multiple targeted searches rather than one broad one.\n"
        "* Cite every source you actually relied on by calling "
        "  task_attach_link with the URL, a short title, and a one-line "
        "  summary of why it's relevant.\n"
        "* Your FINAL message becomes the comment body. Format it as "
        "  a brief headline + 3-6 short bullets / paragraphs. Use "
        "  Markdown — bold the key takeaways. Don't repeat the task "
        "  description back to the user.\n"
        "* If the search tool is not configured, say so plainly in the "
        "  comment so the household admin knows to set up an API key.\n"
        "* If nothing meaningful changed since the last run, say "
        "  exactly that in one sentence — better than padding."
    )

    return prompts.with_safety("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _session() -> Session:
    return SessionLocal()


def _assistant_id_for_family(db: Session, family_id: int) -> Optional[int]:
    row = db.execute(
        select(models.Assistant.assistant_id)
        .where(models.Assistant.family_id == family_id)
        .limit(1)
    ).scalar_one_or_none()
    return row


def _resolve_family_timezone(db: Session, family_id: int) -> str:
    fam = db.get(models.Family, family_id)
    if fam is not None and getattr(fam, "timezone", None):
        return fam.timezone
    return "America/New_York"


def _thinking_model() -> str:
    """Resolve the model used for monitoring runs.

    Honours :data:`Settings.AI_OLLAMA_THINKING_MODEL` when set; falls
    back to the conversational model so a fresh install needs no extra
    Ollama pull.
    """
    settings = get_settings()
    return (settings.AI_OLLAMA_THINKING_MODEL or settings.AI_OLLAMA_MODEL).strip()


__all__ = [
    "run_monitoring_loop",
    "run_now_in_background",
]
