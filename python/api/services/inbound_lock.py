"""Cross-process lock so inbox recovery and the live pollers don't double-reply.

A batch run (``scripts/recover_inbound.py``) and the FastAPI process
can both be alive after a reboot. Postgres advisory locks are tied to
the database connection, so they work across processes on this machine
without a new table. Hold the lock for a whole recovery pass, or for
one live-poller dispatch, and release it before the session closes —
returning a connection to the pool while the lock is still held would
leak it onto the next borrower.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import SessionLocal


logger = logging.getLogger(__name__)

# Stable bigint. Arbitrary; nothing else in this database uses it.
LOCK_KEY = 4_815_162_342


def _lock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})


def _unlock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})


@contextmanager
def hold_recovery_lock() -> Iterator[None]:
    """Block until the inbound recovery lock is ours, then release it."""
    db = SessionLocal()
    _lock(db)
    try:
        yield
    finally:
        try:
            _unlock(db)
        except Exception:  # noqa: BLE001 - never leak the pooled connection
            logger.exception("Failed to release inbound recovery lock")
        db.close()


async def wait_until_set_or_stopped(
    gate: asyncio.Event, stop_event: asyncio.Event
) -> bool:
    """Wait until ``gate`` is set.

    Returns False when ``stop_event`` fires first, so a shutdown during
    the startup recovery check-in doesn't leave the poller tasks stuck.
    """
    stop_task = asyncio.create_task(stop_event.wait())
    gate_task = asyncio.create_task(gate.wait())
    try:
        await asyncio.wait(
            {stop_task, gate_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (stop_task, gate_task):
            if not task.done():
                task.cancel()
        for task in (stop_task, gate_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
    return gate.is_set() and not stop_event.is_set()
