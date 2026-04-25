"""Plan → execute → observe loop for the local AI agent.

The loop is deliberately small. Each turn:

1. Send ``messages`` to the LLM with the available tool catalog.
2. If the model emits ``tool_calls`` we execute them (with timeouts),
   append the tool results to ``messages``, and recurse.
3. If the model emits prose without tool calls, that's the final
   answer — we record it and stop.
4. We stop early at ``max_steps`` to bound runtime even if the model
   loops on tools.

Throughout, we persist every step to the ``agent_steps`` table and
push a corresponding event onto an ``asyncio.Queue`` so a Server-Sent-
Events handler can stream live updates to the browser. The same
events are reconstructable from the database after the fact so the
UI can re-attach to a historical task and replay it identically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from . import ollama, tools


logger = logging.getLogger(__name__)


# Hard upper bound on how many plan→execute cycles a single chat turn
# can run. Most real conversations need 1–3 cycles ("look up Sarah's
# email" → "send the email" → final reply). Five gives genuine
# multi-tool flows headroom without letting a confused model thrash.
DEFAULT_MAX_STEPS = 5

# Per-turn LLM call timeout. Generous because gemma4:26b on a busy
# Mac Studio can take 8-12 s for a structured response with tools.
DEFAULT_LLM_TIMEOUT_S = 90.0

# How long to wait between a transient LLM timeout and the single
# automatic retry. Short on purpose: most ReadTimeouts on a healthy
# host are caused by a temporary GPU stall (another process grabbing
# Metal Performance Shaders for a frame, model weights being paged
# back in, etc.) and clear in seconds. A longer pause just lengthens
# the user-visible "Avi is thinking" gap on email/SMS/Telegram.
LLM_TIMEOUT_RETRY_DELAY_S = 1.5


# ---------------------------------------------------------------------------
# Event payloads — exactly what the SSE channel emits.
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """Single SSE-serialisable event from the agent loop."""

    type: str  # "task_started" | "step" | "delta" | "task_completed" | "task_failed"
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        return f"data: {json.dumps({'type': self.type, **self.payload}, default=str)}\n\n"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def create_task(
    db: Session,
    *,
    family_id: int,
    input_text: str,
    live_session_id: Optional[int] = None,
    person_id: Optional[int] = None,
    kind: str = "chat",
    model: Optional[str] = None,
) -> models.AgentTask:
    task = models.AgentTask(
        family_id=family_id,
        live_session_id=live_session_id,
        person_id=person_id,
        kind=kind,
        status="pending",
        input_text=input_text,
        model=model,
    )
    db.add(task)
    db.flush()
    db.refresh(task)
    return task


def _append_step(
    db: Session,
    task: models.AgentTask,
    *,
    step_index: int,
    step_type: str,
    tool_name: Optional[str] = None,
    tool_input: Optional[Dict[str, Any]] = None,
    tool_output: Optional[Dict[str, Any]] = None,
    content: Optional[str] = None,
    error: Optional[str] = None,
    model: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> models.AgentStep:
    step = models.AgentStep(
        agent_task_id=task.agent_task_id,
        step_index=step_index,
        step_type=step_type,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
        content=content,
        error=error,
        model=model,
        duration_ms=duration_ms,
        created_at=datetime.now(timezone.utc),
    )
    db.add(step)
    db.flush()
    db.refresh(step)
    return step


def _step_to_event_payload(step: models.AgentStep) -> Dict[str, Any]:
    return {
        "step": {
            "agent_step_id": step.agent_step_id,
            "step_index": step.step_index,
            "step_type": step.step_type,
            "tool_name": step.tool_name,
            "tool_input": step.tool_input,
            "tool_output": step.tool_output,
            "content": step.content,
            "error": step.error,
            "model": step.model,
            "duration_ms": step.duration_ms,
            "created_at": step.created_at.isoformat() if step.created_at else None,
        }
    }


# ---------------------------------------------------------------------------
# Agent loop entry point
# ---------------------------------------------------------------------------


async def run_agent(
    *,
    task_id: int,
    family_id: int,
    assistant_id: Optional[int],
    person_id: Optional[int],
    system_prompt: str,
    history: List[Dict[str, str]],
    user_message: str,
    registry: tools.ToolRegistry,
    capabilities: set[str],
    max_steps: int = DEFAULT_MAX_STEPS,
    model_override: Optional[str] = None,
    think: Optional[bool] = None,
    inbound_attachments: Optional[List[tools.InboundAttachmentRef]] = None,
    requestor_is_admin: bool = False,
) -> AsyncIterator[AgentEvent]:
    """Drive a single agent task to completion, yielding SSE events.

    Opens its own :class:`Session` so the loop's lifecycle isn't bound
    to the inbound HTTP request — the same loop is used by both the
    streaming chat path and the (future) background-tasks worker.
    """
    db = SessionLocal()
    started = time.monotonic()
    available_tools = registry.to_ollama_tools(capabilities)
    tool_names_for_log = [t["function"]["name"] for t in available_tools]
    primary_model_for_log = model_override or ollama._model()
    logger.info(
        "[agent] task=%s starting family_id=%s person_id=%s assistant_id=%s "
        "model=%s think=%s max_steps=%s tools=%d inbound_attachments=%d (%s)",
        task_id,
        family_id,
        person_id,
        assistant_id,
        primary_model_for_log,
        think,
        max_steps,
        len(tool_names_for_log),
        len(inbound_attachments or []),
        ",".join(tool_names_for_log),
    )

    try:
        task = db.get(models.AgentTask, task_id)
        if task is None:
            yield AgentEvent(type="task_failed", payload={"error": "task not found"})
            return

        task.status = "running"
        task.started_at = datetime.now(timezone.utc)
        db.flush()
        db.commit()

        yield AgentEvent(
            type="task_started",
            payload={
                "task_id": task.agent_task_id,
                "tools": tool_names_for_log,
                "model": model_override or ollama._model(),
            },
        )

        # Build the conversation we'll keep extending. We replay prior
        # turns exactly as the client sent them; the agent loop only
        # mutates the trailing window (current user → tool calls →
        # tool results → final answer).
        ctx = tools.ToolContext(
            db=db,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=person_id,
            is_admin=requestor_is_admin,
            inbound_attachments=list(inbound_attachments or []),
        )

        messages: List[Dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]}
            for m in history
            if m.get("content")
        ]
        # Make sure the user's current ask is the last turn.
        if not messages or messages[-1].get("content") != user_message:
            messages.append({"role": "user", "content": user_message})

        step_index = 0
        final_text: str = ""
        # ``model_override`` lets specialised callers (e.g. the
        # monitoring scheduler) point one run at the dedicated
        # AI_OLLAMA_THINKING_MODEL without changing the conversational
        # default for the rest of the surface area.
        primary_model = model_override or ollama._model()

        for cycle in range(max_steps):
            cycle_started = time.monotonic()
            # Single in-cycle retry on transient ReadTimeout — the
            # daemon accepted the request but the response stalled
            # (typically a brief GPU contention spike). One retry
            # converts a noisy "agent crashed" into a 1-2s blip the
            # user never sees. We do NOT retry hard errors (4xx, 5xx,
            # ConnectError) because those won't get better in 2 s.
            timeout_attempts = 0
            while True:
                try:
                    turn = await ollama.chat_with_tools(
                        messages,
                        tools=available_tools,
                        system=system_prompt,
                        model=primary_model,
                        timeout_seconds=DEFAULT_LLM_TIMEOUT_S,
                        think=think,
                    )
                    break
                except ollama.OllamaTimeout as exc:
                    timeout_attempts += 1
                    if timeout_attempts > 1:
                        # Already retried once. Give up cleanly with
                        # a typed error step so the email/SMS surface
                        # can render its fallback copy.
                        step = _append_step(
                            db,
                            task,
                            step_index=step_index,
                            step_type="error",
                            error=f"LLM timeout (retried once): {exc}",
                        )
                        db.commit()
                        yield AgentEvent(
                            type="step", payload=_step_to_event_payload(step)
                        )
                        _finalise(
                            db,
                            task,
                            status="failed",
                            error=str(exc),
                            started=started,
                        )
                        yield AgentEvent(
                            type="task_failed",
                            payload={
                                "task_id": task.agent_task_id,
                                "error": str(exc),
                            },
                        )
                        return
                    logger.warning(
                        "Agent task %s: LLM timeout on cycle %d, retrying once: %s",
                        task.agent_task_id,
                        cycle,
                        exc,
                    )
                    await asyncio.sleep(LLM_TIMEOUT_RETRY_DELAY_S)
                    continue
                except ollama.OllamaUnavailable as exc:
                    step = _append_step(
                        db,
                        task,
                        step_index=step_index,
                        step_type="error",
                        error=f"LLM unavailable: {exc}",
                    )
                    db.commit()
                    yield AgentEvent(
                        type="step", payload=_step_to_event_payload(step)
                    )
                    _finalise(
                        db, task, status="failed", error=str(exc), started=started
                    )
                    yield AgentEvent(
                        type="task_failed",
                        payload={"task_id": task.agent_task_id, "error": str(exc)},
                    )
                    return
                except ollama.OllamaError as exc:
                    step = _append_step(
                        db,
                        task,
                        step_index=step_index,
                        step_type="error",
                        error=f"LLM error: {exc}",
                    )
                    db.commit()
                    yield AgentEvent(
                        type="step", payload=_step_to_event_payload(step)
                    )
                    _finalise(
                        db, task, status="failed", error=str(exc), started=started
                    )
                    yield AgentEvent(
                        type="task_failed",
                        payload={"task_id": task.agent_task_id, "error": str(exc)},
                    )
                    return

            cycle_ms = int((time.monotonic() - cycle_started) * 1000)

            # ---- Tool calls path -----------------------------------------
            if turn.tool_calls:
                logger.info(
                    "[agent] task=%s cycle=%d decision=tool_calls model=%s "
                    "duration_ms=%d n_calls=%d tools=[%s]",
                    task_id,
                    cycle,
                    turn.model,
                    cycle_ms,
                    len(turn.tool_calls),
                    ",".join(c.name for c in turn.tool_calls),
                )
                # First record what the model decided to do (one row per call).
                for call in turn.tool_calls:
                    step = _append_step(
                        db,
                        task,
                        step_index=step_index,
                        step_type="tool_call",
                        tool_name=call.name,
                        tool_input=call.arguments,
                        model=turn.model,
                        duration_ms=cycle_ms if step_index == 0 else None,
                    )
                    db.commit()
                    step_index += 1
                    yield AgentEvent(type="step", payload=_step_to_event_payload(step))

                # Now execute them and record results. We append a
                # ``tool`` role message per call so the next LLM turn
                # sees the result in the conversation history.
                # For Ollama compatibility we mirror the calls into the
                # assistant message's ``tool_calls`` field; the same
                # information also lives in the prior step rows.
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": c.name,
                                    "arguments": c.arguments,
                                }
                            }
                            for c in turn.tool_calls
                        ],
                    }
                )

                for call in turn.tool_calls:
                    result = await registry.execute(call.name, call.arguments, ctx)
                    summary = result.summary or _default_result_summary(call.name, result)
                    step = _append_step(
                        db,
                        task,
                        step_index=step_index,
                        step_type="tool_result",
                        tool_name=call.name,
                        tool_input=call.arguments,
                        tool_output=_jsonable(result.output) if result.ok else None,
                        content=summary,
                        error=result.error if not result.ok else None,
                        duration_ms=result.duration_ms,
                    )
                    db.commit()
                    step_index += 1
                    yield AgentEvent(type="step", payload=_step_to_event_payload(step))

                    # Feed the result back to the model.
                    messages.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "content": json.dumps(
                                result.to_payload(), default=str, ensure_ascii=False
                            ),
                        }
                    )

                # Loop again so the model can use the tool output.
                continue

            # ---- Final answer path ---------------------------------------
            final_text = turn.content or ""
            logger.info(
                "[agent] task=%s cycle=%d decision=final model=%s "
                "duration_ms=%d reply_chars=%d",
                task_id,
                cycle,
                turn.model,
                cycle_ms,
                len(final_text),
            )
            step = _append_step(
                db,
                task,
                step_index=step_index,
                step_type="final",
                content=final_text,
                model=turn.model,
                duration_ms=cycle_ms,
            )
            db.commit()
            step_index += 1
            yield AgentEvent(type="step", payload=_step_to_event_payload(step))
            # Also emit a delta event with the full final text so chat
            # UI that renders deltas appends it without special-casing.
            if final_text:
                yield AgentEvent(type="delta", payload={"delta": final_text})
            break

        else:
            # Loop hit max_steps without producing a final answer.
            error_msg = (
                f"Agent did not converge within {max_steps} steps."
            )
            logger.warning(
                "[agent] task=%s exhausted max_steps=%d without final answer",
                task_id,
                max_steps,
            )
            step = _append_step(
                db,
                task,
                step_index=step_index,
                step_type="error",
                error=error_msg,
            )
            db.commit()
            yield AgentEvent(type="step", payload=_step_to_event_payload(step))
            _finalise(db, task, status="failed", error=error_msg, started=started)
            yield AgentEvent(
                type="task_failed",
                payload={"task_id": task.agent_task_id, "error": error_msg},
            )
            return

        _finalise(
            db,
            task,
            status="succeeded",
            summary=final_text or "(no response)",
            started=started,
        )
        logger.info(
            "[agent] task=%s completed cycles=%d total_ms=%d reply_chars=%d",
            task_id,
            step_index,
            task.duration_ms or int((time.monotonic() - started) * 1000),
            len(final_text),
        )
        yield AgentEvent(
            type="task_completed",
            payload={
                "task_id": task.agent_task_id,
                "summary": final_text,
                "duration_ms": task.duration_ms,
            },
        )

    except Exception as exc:  # noqa: BLE001 - surface to client
        logger.exception("Agent loop crashed for task %s", task_id)
        try:
            task = db.get(models.AgentTask, task_id)
            if task is not None:
                _finalise(db, task, status="failed", error=str(exc), started=started)
        except Exception:  # noqa: BLE001
            db.rollback()
        yield AgentEvent(
            type="task_failed", payload={"task_id": task_id, "error": str(exc)}
        )
    finally:
        db.close()


def _finalise(
    db: Session,
    task: models.AgentTask,
    *,
    status: str,
    started: float,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    task.status = status
    task.completed_at = datetime.now(timezone.utc)
    task.duration_ms = int((time.monotonic() - started) * 1000)
    if summary is not None:
        task.summary = summary
    if error is not None:
        task.error = error
    db.flush()
    db.commit()


def _jsonable(value: Any) -> Any:
    """Force a value into something JSONB will accept."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        try:
            json.dumps(value, default=str)
            return value
        except TypeError:
            return json.loads(json.dumps(value, default=str))
    return json.loads(json.dumps(value, default=str))


def _default_result_summary(tool_name: str, result: tools.ToolResult) -> str:
    if not result.ok:
        return f"{tool_name}: error — {result.error}"
    out = result.output
    if isinstance(out, list):
        return f"{tool_name}: {len(out)} result(s)"
    if isinstance(out, dict):
        keys = list(out.keys())[:4]
        return f"{tool_name}: ok ({', '.join(keys)})"
    return f"{tool_name}: ok"


__all__ = [
    "AgentEvent",
    "create_task",
    "run_agent",
]
