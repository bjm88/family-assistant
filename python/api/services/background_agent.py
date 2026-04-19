"""Bounded background runner for blocking agent loops.

Why this module exists
----------------------
The push-style messaging surfaces (Telegram, SMS) currently call
``_run_agent_to_completion`` synchronously inside their per-update
worker thread, then send the resulting text as a reply. That's simple
but means the thread is parked for 5–30 seconds per inbound — and
during that wait the user's chat is silent.

The "fast-ack" pattern (see :mod:`api.ai.fast_ack`) needs the heavy
agent to run *concurrently* with a watchdog that fires a quick "I'm
on it" reply if the heavy agent doesn't finish in time. To do that
cleanly we need a primitive that:

* Runs an arbitrary blocking callable on a **bounded** worker pool
  (so a flood of inbound messages can't fork-bomb us).
* Returns a future the caller can ``.result(timeout=N)`` to race
  against, then ``.result()`` again to block until done.
* Survives across multiple inbound calls — the executor lives at
  module scope and is shared by every caller.

That's it. This module is intentionally tiny — there's no need for a
job queue, persistence, or retries; the audit row already records the
outcome and a crashed agent loop never crashes the inbox poller.

Capacity sizing
---------------
``DEFAULT_MAX_WORKERS`` is sized for a household-scale install: small
enough that we don't run multiple 26B-parameter generations in
parallel (Ollama serialises GPU access anyway, so extra workers just
waste RAM), but large enough that an SMS arriving while a Telegram
agent is mid-flight doesn't have to wait for the Telegram one to
finish before its own ack can fire. Override via
:setting:`AI_BACKGROUND_AGENT_MAX_WORKERS` if your hardware can
genuinely run multiple LLM passes concurrently.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

from ..config import get_settings


logger = logging.getLogger(__name__)


DEFAULT_MAX_WORKERS = 4


_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


T = TypeVar("T")


def _resolve_max_workers() -> int:
    """Read the override from settings, falling back to the default.

    Kept as a separate helper so tests can monkey-patch settings
    between executor recreations.
    """
    settings = get_settings()
    val = getattr(settings, "AI_BACKGROUND_AGENT_MAX_WORKERS", None)
    if isinstance(val, int) and val > 0:
        return val
    return DEFAULT_MAX_WORKERS


def _get_executor() -> ThreadPoolExecutor:
    """Lazily build (or reuse) the module-level executor.

    We construct the pool on first use rather than at import time so
    test runs and one-off CLI invocations don't pay for spinning up
    background threads they'll never use.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = _resolve_max_workers()
            _executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="agent-bg",
            )
            logger.info(
                "background_agent: executor up with max_workers=%d",
                workers,
            )
        return _executor


def submit(work: Callable[[], T]) -> "Future[T]":
    """Run ``work()`` on the shared background pool; return a Future.

    ``work`` should be a zero-arg callable (use ``functools.partial``
    or a closure to bind arguments). Whatever it returns lands on the
    returned future as ``future.result()``; whatever it raises lands
    as ``future.exception()``.

    Typical use::

        future = background_agent.submit(
            lambda: _run_agent_to_completion(...)
        )
        try:
            text = future.result(timeout=settings.AI_FAST_ACK_AFTER_SECONDS)
        except FuturesTimeoutError:
            send_ack(...)
            text = future.result()  # blocks until heavy agent finishes
        send_final_reply(text)
    """
    return _get_executor().submit(work)


def shutdown(wait: bool = True) -> None:
    """Tear down the executor — only used by tests + clean shutdown.

    The FastAPI lifespan does not currently call this because the
    process is going away anyway and ``daemon=True`` workers will be
    killed automatically. Exposed so test fixtures can reset state
    between cases.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=False)
            _executor = None


__all__ = ["submit", "shutdown", "DEFAULT_MAX_WORKERS"]
