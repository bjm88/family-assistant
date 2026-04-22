"""Run :func:`api.ai.agent.run_agent` to completion from sync code.

Every inbound surface that talks to the agent from a *background
thread* needs to do exactly the same thing: kick the async generator,
watch for ``task_completed`` / ``task_failed`` events, and return the
final reply text once the loop is done. Each surface used to keep its
own near-identical ``_drain`` helper (sms / email / telegram /
monitoring) — same shape, slightly different fallback strings. This
module is the single shared implementation so the four call sites can
focus on their own UX wording instead of re-implementing event
draining.

The two-step ``asyncio.run`` → ``new_event_loop`` fallback is
important: ``asyncio.run`` raises ``RuntimeError`` when invoked from a
thread that already owns a running loop. Inbound webhooks usually run
the drain via ``asyncio.to_thread`` so they're on a fresh worker
thread and ``asyncio.run`` succeeds; the fallback exists for
defensive resilience and is exercised by the monitoring scheduler
when run inline from tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional

from . import agent as agent_loop
from .tools import ToolRegistry


@dataclass(frozen=True)
class AgentDrainResult:
    """Outcome of draining the agent generator."""

    final_text: str
    error_text: Optional[str]


def drain_agent_sync(
    *,
    task_id: int,
    family_id: int,
    assistant_id: Optional[int],
    person_id: Optional[int],
    system_prompt: str,
    history: List[dict],
    user_message: str,
    registry: ToolRegistry,
    capabilities: set[str],
    extra_run_kwargs: Optional[dict[str, Any]] = None,
) -> AgentDrainResult:
    """Drive :func:`agent.run_agent` to completion and return its reply.

    Parameters mirror :func:`api.ai.agent.run_agent` 1:1. Pass
    ``extra_run_kwargs`` for surface-specific overrides like
    ``model_override`` / ``think`` / ``max_steps`` (used by the
    monitoring scheduler for its dedicated thinking model and a
    larger step budget).

    Returns an :class:`AgentDrainResult` with the parsed final reply
    and the raw error from ``task_failed`` (if any). Callers decide
    what to do on error — chat surfaces typically substitute a
    canonical apology, monitoring re-raises so the failure shows up
    on the task row.
    """
    final_text = ""
    error_text: Optional[str] = None
    extras = dict(extra_run_kwargs or {})

    async def _run() -> None:
        nonlocal final_text, error_text
        async for event in agent_loop.run_agent(
            task_id=task_id,
            family_id=family_id,
            assistant_id=assistant_id,
            person_id=person_id,
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
            registry=registry,
            capabilities=capabilities,
            **extras,
        ):
            if event.type == "task_completed":
                final_text = (event.payload.get("summary") or "").strip()
            elif event.type == "task_failed":
                error_text = str(event.payload.get("error") or "Agent failed.")

    try:
        asyncio.run(_run())
    except RuntimeError:
        # Caller is already inside a running loop on this thread.
        # Spin up a private one so we can still drive the generator
        # to completion.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    return AgentDrainResult(final_text=final_text, error_text=error_text)


__all__ = ["AgentDrainResult", "drain_agent_sync"]
