"""Live system-status endpoint backing the admin Status page.

Every visit to ``GET /api/admin/status`` triggers a fresh fan-out probe of
all the local services Avi depends on (Postgres, Ollama, ngrok, the React
dev server, the LLM itself). The endpoint is deliberately not cached and
not rate-limited because the page that calls it does so on mount, and an
operator who just hit "Refresh" expects an up-to-the-second answer.

Latency budget: the slowest single check (typically the AI generate ping
against a 26 B model) is ~3-6 s warm. Everything else finishes in tens of
milliseconds, so the total wall-clock for this endpoint is bounded by the
slowest probe rather than the sum.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..services.system_status import (
    SystemStatusReport,
    gather_status_report,
)


router = APIRouter(prefix="/status", tags=["status"])


@router.get(
    "",
    response_model=SystemStatusReport,
    summary="Run every health probe and return a single roll-up.",
)
async def system_status() -> SystemStatusReport:
    return await gather_status_report()
