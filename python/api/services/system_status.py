"""Live health probes for the local services Avi depends on.

This module is intentionally read-only and side-effect-free. Each ``check_*``
coroutine actively pokes one piece of infrastructure (Postgres, Ollama, ngrok,
the React dev server, …) and returns a structured ``StatusCheck`` describing
what it found. The router (:mod:`api.routers.status`) fans them out
concurrently so the whole status page renders in roughly the latency of the
slowest single check.

Design rules every check obeys:

* **Bounded latency.** Every network probe has a hard timeout (defaults
  range from 2 s for cheap local sockets up to 12 s for the Ollama
  generate ping which has to wake a 26 B model). A status page that
  hangs is worse than one that says "down".

* **No exceptions escape.** A check that raises is automatically wrapped
  by :func:`_safe` into a ``status="down"`` row — the page must render
  end-to-end even if Postgres is on fire.

* **Actionable hints.** When something is down we include a short
  ``hint`` string the operator can copy/paste into a terminal (e.g.
  ``brew services start postgresql@16``). The status page is the first
  place we look at 7am, it should help us fix things.

Every check returns latency in milliseconds plus a free-form ``detail``
dict the UI renders as a key/value table beneath each row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, engine

logger = logging.getLogger(__name__)


StatusLevel = Literal["ok", "degraded", "down", "unknown"]


class StatusCheck(BaseModel):
    """One row on the status page."""

    key: str = Field(..., description="Stable identifier (used as React key).")
    label: str = Field(..., description="Human-readable name, e.g. 'Postgres'.")
    status: StatusLevel
    latency_ms: Optional[float] = None
    summary: str = Field(
        ..., description="One-liner shown next to the status pill."
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured key/value pairs the UI renders as a table.",
    )
    hint: Optional[str] = Field(
        default=None,
        description="If status != 'ok', a short fix tip (shell command, URL, …).",
    )
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SystemStatusReport(BaseModel):
    """Aggregate response returned by the status endpoint."""

    overall: StatusLevel
    generated_at: datetime
    checks: list[StatusCheck]


# ---------------------------------------------------------------------------
# Process metadata — captured at import time so the API check can report
# wall-clock uptime instead of a meaningless "running now" message.
# ---------------------------------------------------------------------------

_PROCESS_STARTED_AT = datetime.now(timezone.utc)
_PROCESS_PID = os.getpid()


def _format_duration(seconds: float) -> str:
    """Render a duration as ``2d 3h``, ``45m``, ``32s`` etc."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------


async def _safe(
    key: str,
    label: str,
    coro_factory: Callable[[], Awaitable[StatusCheck]],
) -> StatusCheck:
    """Run a check, converting any exception into a 'down' result.

    Each individual check is responsible for setting its own ``hint`` and
    populating ``detail``. This wrapper exists so the status endpoint is
    crash-proof: even a programming error inside one probe still renders
    the rest of the page.
    """
    started = _now_ms()
    try:
        return await coro_factory()
    except Exception as exc:  # noqa: BLE001 - status page must never crash
        logger.exception("Status check %s raised", key)
        return StatusCheck(
            key=key,
            label=label,
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Check raised: {exc.__class__.__name__}",
            detail={"error": str(exc)},
            hint="See backend logs (logs/backend.log) for the full traceback.",
        )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


async def check_api() -> StatusCheck:
    """The endpoint replied → the API is by definition up.

    We use this slot to surface useful process metadata (uptime, pid,
    log level) without making the operator open a terminal.
    """
    settings = get_settings()
    uptime_seconds = (
        datetime.now(timezone.utc) - _PROCESS_STARTED_AT
    ).total_seconds()
    return StatusCheck(
        key="api",
        label="FastAPI backend",
        status="ok",
        latency_ms=0.0,
        summary=f"Up for {_format_duration(uptime_seconds)}",
        detail={
            "pid": _PROCESS_PID,
            "started_at": _PROCESS_STARTED_AT.isoformat(),
            "log_level": os.environ.get("FA_LOG_LEVEL", "INFO"),
            "cors_origins": settings.cors_origins,
        },
    )


async def check_postgres() -> StatusCheck:
    """Open a connection, run ``SELECT version()`` + report DB size.

    SQLAlchemy's ``engine`` is sync so we run it in a worker thread to
    avoid blocking the event loop. ``pool_pre_ping`` already checks
    socket health on checkout, but we want fresh latency numbers, so
    each visit opens a real query.
    """
    started = _now_ms()
    settings = get_settings()

    def _query() -> dict[str, Any]:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT version() AS version, "
                    "pg_size_pretty(pg_database_size(current_database())) AS size, "
                    "current_database() AS db"
                )
            ).mappings().one()
            return dict(row)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_query), timeout=4.0)
    except asyncio.TimeoutError:
        return StatusCheck(
            key="postgres",
            label="Postgres",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary="Connection timed out after 4 s",
            detail={
                "host": settings.FA_DB_HOST,
                "port": settings.FA_DB_PORT,
                "database": settings.FA_DB_NAME,
            },
            hint=(
                "Is Postgres running? "
                "`brew services start postgresql@16` or "
                "`pg_ctl -D /opt/homebrew/var/postgresql@16 start`."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="postgres",
            label="Postgres",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Connection failed: {exc.__class__.__name__}",
            detail={
                "host": settings.FA_DB_HOST,
                "port": settings.FA_DB_PORT,
                "database": settings.FA_DB_NAME,
                "error": str(exc),
            },
            hint=(
                "Verify FA_DB_* in .env and that the user has CONNECT "
                "permission on the database."
            ),
        )

    latency = round(_now_ms() - started, 1)
    # ``version()`` returns the full banner ("PostgreSQL 16.4 on aarch64-…") —
    # trim it to the first useful chunk for a tidy summary.
    version_short = " ".join(info["version"].split()[:2])
    return StatusCheck(
        key="postgres",
        label="Postgres",
        status="ok",
        latency_ms=latency,
        summary=f"{version_short} · {info['size']}",
        detail={
            "database": info["db"],
            "host": settings.FA_DB_HOST,
            "port": settings.FA_DB_PORT,
            "user": settings.FA_DB_USER,
            "size": info["size"],
            "version": info["version"],
        },
    )


async def _ollama_get_tags(client: httpx.AsyncClient, host: str) -> list[dict[str, Any]]:
    """Return the parsed ``models`` array from ``GET /api/tags``."""
    resp = await client.get(f"{host.rstrip('/')}/api/tags", timeout=4.0)
    resp.raise_for_status()
    payload = resp.json()
    return list(payload.get("models", []))


async def check_ollama_daemon() -> StatusCheck:
    """Verify the Ollama HTTP server itself responds at all."""
    settings = get_settings()
    host = settings.AI_OLLAMA_HOST
    started = _now_ms()
    try:
        async with httpx.AsyncClient() as client:
            await _ollama_get_tags(client, host)
    except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
        return StatusCheck(
            key="ollama_daemon",
            label="Ollama daemon",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"No response from {host}",
            detail={"host": host, "error": str(exc)},
            hint=(
                "Start Ollama: open the Ollama app, or run "
                "`OLLAMA_HOST=0.0.0.0 ollama serve` in a terminal."
            ),
        )

    return StatusCheck(
        key="ollama_daemon",
        label="Ollama daemon",
        status="ok",
        latency_ms=round(_now_ms() - started, 1),
        summary=f"Reachable at {host}",
        detail={"host": host},
    )


async def check_ollama_models() -> StatusCheck:
    """Confirm the configured chat + fast models are actually pulled.

    A common foot-gun is changing ``AI_OLLAMA_MODEL`` in .env and
    forgetting to ``ollama pull`` the new tag — every chat then 404s
    inside the agent loop. This check makes that mismatch obvious.
    """
    settings = get_settings()
    host = settings.AI_OLLAMA_HOST
    required = [t for t in (settings.AI_OLLAMA_MODEL,) if t]
    optional = [t for t in (settings.AI_OLLAMA_FAST_MODEL,) if t]

    started = _now_ms()
    try:
        async with httpx.AsyncClient() as client:
            models = await _ollama_get_tags(client, host)
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="ollama_models",
            label="Ollama models",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary="Could not list models — daemon unreachable.",
            detail={"error": str(exc)},
            hint="Fix the Ollama daemon first; this check derives from /api/tags.",
        )

    installed = sorted({str(m.get("name", "")) for m in models if m.get("name")})
    # A pulled tag in Ollama always carries a trailing version, e.g.
    # ``gemma4:26b``. Users sometimes type just ``gemma4`` in .env, so
    # we accept either an exact match or a "name starts with the
    # configured tag plus ':'" prefix match.
    def _present(tag: str) -> bool:
        if tag in installed:
            return True
        return any(name == tag or name.startswith(tag + ":") for name in installed)

    missing_required = [t for t in required if not _present(t)]
    missing_optional = [t for t in optional if not _present(t)]

    if missing_required:
        return StatusCheck(
            key="ollama_models",
            label="Ollama models",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Missing required model: {', '.join(missing_required)}",
            detail={
                "installed": installed,
                "required": required,
                "optional": optional,
                "missing_required": missing_required,
                "missing_optional": missing_optional,
            },
            hint=(
                f"Pull it: `ollama pull {missing_required[0]}` "
                "(then refresh this page)."
            ),
        )

    if missing_optional:
        return StatusCheck(
            key="ollama_models",
            label="Ollama models",
            status="degraded",
            latency_ms=round(_now_ms() - started, 1),
            summary=(
                f"{len(installed)} models installed · fast model "
                f"'{missing_optional[0]}' missing"
            ),
            detail={
                "installed": installed,
                "required": required,
                "optional": optional,
                "missing_optional": missing_optional,
            },
            hint=(
                f"Optional speed boost: `ollama pull {missing_optional[0]}`. "
                "The agent will fall back to the main chat model in the meantime."
            ),
        )

    return StatusCheck(
        key="ollama_models",
        label="Ollama models",
        status="ok",
        latency_ms=round(_now_ms() - started, 1),
        summary=f"{len(installed)} model(s) installed; chat + fast both present.",
        detail={
            "installed": installed,
            "chat_model": settings.AI_OLLAMA_MODEL,
            "fast_model": settings.AI_OLLAMA_FAST_MODEL,
        },
    )


async def check_ai_agent() -> StatusCheck:
    """End-to-end agent ping: chat with the configured model.

    A green ``ollama_models`` row proves the tag exists on disk; this
    proves the model can actually load and produce tokens. We hit
    ``/api/chat`` (not ``/api/generate``) because that path applies the
    model's chat template — necessary for instruction-tuned and
    thinking-style models like Gemma 4 where the raw generate endpoint
    leaves the chat scaffold to the caller.

    Some thinking models route their first tokens into a separate
    ``message.thinking`` field; we count that as success too. The point
    is "did weights load and produce output", not "did the model obey
    a specific instruction".
    """
    settings = get_settings()
    host = settings.AI_OLLAMA_HOST
    model = settings.AI_OLLAMA_MODEL
    started = _now_ms()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Reply with the single word: ok. "
                    "No explanation."
                ),
            }
        ],
        "stream": False,
        # 64 tokens is enough headroom for a small thinking budget +
        # the actual reply on every model family we ship today
        # (Gemma 4 26B, Llama 3.1, Qwen 2.5, …).
        "options": {"num_predict": 64, "temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{host.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return StatusCheck(
            key="ai_agent",
            label="AI agent",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"HTTP {exc.response.status_code} from /api/chat",
            detail={
                "model": model,
                "host": host,
                "body": exc.response.text[:400],
            },
            hint=(
                f"Ollama could not serve the model. Try "
                f"`ollama run {model}` to see the full error."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="ai_agent",
            label="AI agent",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Chat call failed: {exc.__class__.__name__}",
            detail={"model": model, "host": host, "error": str(exc)},
            hint=f"Run `ollama run {model}` and confirm it loads.",
        )

    latency = round(_now_ms() - started, 1)
    message = data.get("message") or {}
    content = (message.get("content") or "").strip()
    thinking = (message.get("thinking") or "").strip()
    eval_count = int(data.get("eval_count") or 0)

    if not content and not thinking and eval_count == 0:
        return StatusCheck(
            key="ai_agent",
            label="AI agent",
            status="degraded",
            latency_ms=latency,
            summary="Model loaded but produced 0 tokens.",
            detail={"model": model, "raw": data},
            hint=(
                "Try restarting Ollama (`ollama stop` / restart the app) — "
                "weights may be in a bad state."
            ),
        )

    # Pick the most useful preview to show in the summary line.
    preview = (content or thinking).replace("\n", " ").strip()
    if len(preview) > 60:
        preview = preview[:57] + "…"
    flavor = "" if content else " (thinking only)"

    # Anything north of ~8 s on a tiny prompt is suspicious — usually a
    # cold-start, GPU contention, or the wrong execution provider.
    level: StatusLevel = "ok" if latency < 8000 else "degraded"
    return StatusCheck(
        key="ai_agent",
        label="AI agent",
        status=level,
        latency_ms=latency,
        summary=(
            f'Model "{model}" replied with "{preview}"{flavor} '
            f"in {int(latency)} ms"
        ),
        detail={
            "model": model,
            "host": host,
            "response": content,
            "thinking_preview": thinking[:200] if thinking else None,
            "eval_count": eval_count,
            "prompt_eval_count": data.get("prompt_eval_count"),
            "total_duration_ms": (
                round(int(data["total_duration"]) / 1_000_000, 1)
                if "total_duration" in data
                else None
            ),
        },
        hint=(
            "Latency is high — the first call after a long idle period "
            "warms the model. Re-check after the next request."
            if level == "degraded"
            else None
        ),
    )


async def check_ngrok_local() -> StatusCheck:
    """Probe the ngrok agent's local web interface (port 4040)."""
    started = _now_ms()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "http://localhost:4040/api/tunnels", timeout=2.0
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="ngrok_local",
            label="ngrok agent",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary="No agent listening on http://localhost:4040",
            detail={"error": str(exc)},
            hint=(
                "Start the tunnel: "
                "`ngrok http --url=$NGROK_DOMAIN 8000` "
                "(in a dedicated terminal so it stays alive)."
            ),
        )

    tunnels = list(data.get("tunnels", []) or [])
    if not tunnels:
        return StatusCheck(
            key="ngrok_local",
            label="ngrok agent",
            status="degraded",
            latency_ms=round(_now_ms() - started, 1),
            summary="Agent is running but has no active tunnels.",
            detail={"agent_status": "running"},
            hint="Re-run your `ngrok http …` command — the tunnel exited.",
        )

    public_urls = [t.get("public_url") for t in tunnels if t.get("public_url")]
    forwarded_to = [t.get("config", {}).get("addr") for t in tunnels]
    return StatusCheck(
        key="ngrok_local",
        label="ngrok agent",
        status="ok",
        latency_ms=round(_now_ms() - started, 1),
        summary=f"{len(tunnels)} tunnel(s) active",
        detail={
            "public_urls": public_urls,
            "forwarded_to": forwarded_to,
            "tunnels": [
                {
                    "name": t.get("name"),
                    "proto": t.get("proto"),
                    "public_url": t.get("public_url"),
                    "addr": t.get("config", {}).get("addr"),
                }
                for t in tunnels
            ],
        },
    )


async def check_ngrok_public() -> StatusCheck:
    """Verify the public URL Twilio + email links use is actually serving us.

    We try, in priority order:

    1. ``TWILIO_WEBHOOK_PUBLIC_URL`` (full URL → derive its origin)
    2. ``NGROK_DOMAIN`` (bare hostname → assume https)

    The probe is a ``GET /api/health`` against that origin. Anything 2xx
    is "ok"; a 5xx is "degraded" (tunnel is up, backend is angry); any
    network error is "down".
    """
    settings = get_settings()
    public_origin = _derive_public_origin(settings.TWILIO_WEBHOOK_PUBLIC_URL)
    if not public_origin and os.environ.get("NGROK_DOMAIN"):
        public_origin = f"https://{os.environ['NGROK_DOMAIN'].strip('/')}"

    if not public_origin:
        return StatusCheck(
            key="ngrok_public",
            label="ngrok public URL",
            status="unknown",
            latency_ms=None,
            summary="No public URL configured.",
            detail={
                "twilio_webhook_public_url": settings.TWILIO_WEBHOOK_PUBLIC_URL,
                "ngrok_domain": os.environ.get("NGROK_DOMAIN"),
            },
            hint=(
                "Set NGROK_DOMAIN or TWILIO_WEBHOOK_PUBLIC_URL in .env so "
                "we know which URL Twilio (and the legal pages) use."
            ),
        )

    probe_url = f"{public_origin}/api/health"
    started = _now_ms()
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(probe_url, timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="ngrok_public",
            label="ngrok public URL",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Could not reach {public_origin}",
            detail={"probe_url": probe_url, "error": str(exc)},
            hint=(
                "Make sure the ngrok agent is up AND pointed at port 8000: "
                "`ngrok http --url=$NGROK_DOMAIN 8000`."
            ),
        )

    latency = round(_now_ms() - started, 1)
    if resp.status_code >= 500:
        return StatusCheck(
            key="ngrok_public",
            label="ngrok public URL",
            status="degraded",
            latency_ms=latency,
            summary=f"Tunnel reachable but {probe_url} returned {resp.status_code}.",
            detail={"probe_url": probe_url, "status_code": resp.status_code},
            hint="Check backend logs — the tunnel made it through but FastAPI errored.",
        )
    if resp.status_code >= 400:
        return StatusCheck(
            key="ngrok_public",
            label="ngrok public URL",
            status="degraded",
            latency_ms=latency,
            summary=f"{probe_url} responded {resp.status_code}.",
            detail={"probe_url": probe_url, "status_code": resp.status_code},
            hint="Tunnel is up, but the health endpoint is non-2xx — odd; investigate.",
        )

    return StatusCheck(
        key="ngrok_public",
        label="ngrok public URL",
        status="ok",
        latency_ms=latency,
        summary=f"{public_origin} → {resp.status_code} in {int(latency)} ms",
        detail={
            "probe_url": probe_url,
            "status_code": resp.status_code,
            "ngrok_domain": os.environ.get("NGROK_DOMAIN"),
        },
    )


def _derive_public_origin(url_or_none: Optional[str]) -> Optional[str]:
    """Strip a configured webhook URL down to ``scheme://host``."""
    if not url_or_none:
        return None
    parsed = urlparse(url_or_none)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


async def check_ui() -> StatusCheck:
    """Probe the React dev server using the configured CORS origin.

    In production the UI is reachable behind the same domain as the API
    so this becomes a no-op (origin = self → trivially up). In dev the
    UI runs at ``http://localhost:5173`` and may be down without the API
    noticing — this check makes that explicit.
    """
    settings = get_settings()
    origins = settings.cors_origins
    if not origins:
        return StatusCheck(
            key="ui",
            label="React UI",
            status="unknown",
            summary="No CORS origin configured.",
            detail={"cors_origins": origins},
            hint="Set FA_CORS_ORIGINS in .env (e.g. http://localhost:5173).",
        )

    target = origins[0]
    started = _now_ms()
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(target, timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="ui",
            label="React UI",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"No response from {target}",
            detail={"target": target, "error": str(exc)},
            hint=(
                "Start the dev server: "
                "`cd ui/react && npm run dev`."
            ),
        )

    latency = round(_now_ms() - started, 1)
    if resp.status_code >= 500:
        return StatusCheck(
            key="ui",
            label="React UI",
            status="degraded",
            latency_ms=latency,
            summary=f"{target} returned {resp.status_code}",
            detail={"target": target, "status_code": resp.status_code},
        )
    return StatusCheck(
        key="ui",
        label="React UI",
        status="ok",
        latency_ms=latency,
        summary=f"{target} → {resp.status_code} in {int(latency)} ms",
        detail={"target": target, "status_code": resp.status_code},
    )


async def check_gemini_api() -> StatusCheck:
    """Verify the Gemini (Generative Language) API key is live.

    We hit ``GET /v1beta/models`` because it's free, lightweight, and
    returns a model list which is genuinely useful diagnostic info — if
    the key is valid but the project has the API disabled we'll see
    ``PERMISSION_DENIED`` here long before any agent code trips over it.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or get_settings().GEMINI_API_KEY
    if not api_key:
        return StatusCheck(
            key="gemini_api",
            label="Gemini API",
            status="unknown",
            summary="GEMINI_API_KEY not set in .env.",
            detail={},
            hint=(
                "Generate a key at https://aistudio.google.com/app/apikey "
                "and put it in .env as GEMINI_API_KEY=…"
            ),
        )

    started = _now_ms()
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(url, params={"key": api_key})
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="gemini_api",
            label="Gemini API",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"Could not reach generativelanguage.googleapis.com",
            detail={"error": str(exc)},
            hint="Check your internet connection and DNS.",
        )

    latency = round(_now_ms() - started, 1)
    if resp.status_code in (401, 403):
        # Try to extract Google's structured error message — much more
        # actionable than a generic "401 Unauthorized".
        try:
            err = resp.json().get("error", {})
            err_msg = err.get("message") or resp.text[:200]
            err_status = err.get("status")
        except Exception:  # noqa: BLE001
            err_msg = resp.text[:200]
            err_status = None
        return StatusCheck(
            key="gemini_api",
            label="Gemini API",
            status="down",
            latency_ms=latency,
            summary=f"Auth failed ({resp.status_code}): {err_msg[:80]}",
            detail={
                "status_code": resp.status_code,
                "google_status": err_status,
                "message": err_msg,
            },
            hint=(
                "Either the key is wrong/revoked or the Generative "
                "Language API is disabled in the GEMINI_PROJECT_ID "
                "Google Cloud project. Enable it at "
                "https://console.cloud.google.com/apis/library/"
                "generativelanguage.googleapis.com"
            ),
        )
    if resp.status_code >= 400:
        return StatusCheck(
            key="gemini_api",
            label="Gemini API",
            status="down",
            latency_ms=latency,
            summary=f"HTTP {resp.status_code} from /v1beta/models",
            detail={"status_code": resp.status_code, "body": resp.text[:200]},
        )

    payload = resp.json()
    models_list = payload.get("models") or []
    # Surface a few familiar names if present so the operator can sanity
    # check that the expected family of models is available to them.
    favored = sorted(
        {
            m.get("name", "").removeprefix("models/")
            for m in models_list
            if any(
                tag in m.get("name", "")
                for tag in ("gemini-1.5", "gemini-2", "embedding-001")
            )
        }
    )
    return StatusCheck(
        key="gemini_api",
        label="Gemini API",
        status="ok",
        latency_ms=latency,
        summary=f"Key valid · {len(models_list)} model(s) available",
        detail={
            "model_count": len(models_list),
            "notable_models": favored[:8],
            "project_id": get_settings().GEMINI_PROJECT_ID,
            "key_suffix": f"…{api_key[-6:]}",
        },
    )


def _check_google_for_assistant(
    assistant_id: int,
    granted_email: Optional[str],
) -> dict[str, Any]:
    """Synchronous Gmail + Calendar ping for a single assistant.

    Run inside a worker thread because the Google API client is sync.
    Opens its own DB session because the FastAPI request session may
    have already been closed by the time the worker starts.
    """
    # Local imports keep import-time cost off the API hot path; these
    # libraries pull in protobuf and a discovery cache.
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.errors import HttpError  # type: ignore

    from ..integrations import google_oauth

    result: dict[str, Any] = {
        "assistant_id": assistant_id,
        "granted_email": granted_email,
        "gmail": "unknown",
        "calendar": "unknown",
        "errors": [],
    }

    db: Session = SessionLocal()
    try:
        try:
            _, creds = google_oauth.load_credentials(db, assistant_id)
            db.commit()
        except google_oauth.GoogleNotConnected as exc:
            result["gmail"] = "not_connected"
            result["calendar"] = "not_connected"
            result["errors"].append(str(exc))
            return result
        except Exception as exc:  # noqa: BLE001
            result["gmail"] = "down"
            result["calendar"] = "down"
            result["errors"].append(f"credentials: {exc}")
            return result

        try:
            gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = gmail.users().getProfile(userId="me").execute()
            result["gmail"] = "ok"
            result["gmail_email"] = profile.get("emailAddress")
            result["gmail_messages_total"] = profile.get("messagesTotal")
        except HttpError as exc:
            result["gmail"] = "down"
            result["errors"].append(f"gmail: {exc.status_code} {exc.reason}")
        except Exception as exc:  # noqa: BLE001
            result["gmail"] = "down"
            result["errors"].append(f"gmail: {exc}")

        try:
            cal = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
            cal_list = cal.calendarList().list(maxResults=10).execute()
            result["calendar"] = "ok"
            items = cal_list.get("items", []) or []
            result["calendar_count"] = len(items)
            result["calendar_primary"] = next(
                (
                    c.get("summary")
                    for c in items
                    if c.get("primary")
                ),
                None,
            )
        except HttpError as exc:
            result["calendar"] = "down"
            result["errors"].append(f"calendar: {exc.status_code} {exc.reason}")
        except Exception as exc:  # noqa: BLE001
            result["calendar"] = "down"
            result["errors"].append(f"calendar: {exc}")
    finally:
        db.close()

    return result


async def check_google_apis() -> StatusCheck:
    """Verify each assistant's Gmail + Calendar connection actually works.

    A connected account in the database isn't enough — refresh tokens
    can be revoked, scopes can change out from under us, and a network
    glitch during refresh produces the same opaque "no Avi reply"
    symptom as a missing OAuth row. Make every assistant's connection
    a first-class status row instead.
    """
    from .. import models  # local import to keep the orchestrator import-cheap

    started = _now_ms()
    settings = get_settings()
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        return StatusCheck(
            key="google_apis",
            label="Google APIs (Gmail + Calendar)",
            status="unknown",
            summary="OAuth client not configured.",
            detail={
                "client_id_set": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
                "client_secret_set": bool(settings.GOOGLE_OAUTH_CLIENT_SECRET),
            },
            hint=(
                "Set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET in "
                ".env (Google Cloud Console → Credentials → OAuth client)."
            ),
        )

    # Gather connected-assistant ids in a worker thread so the sync ORM
    # never blocks the event loop.
    def _list_assistants() -> list[tuple[int, Optional[str]]]:
        with SessionLocal() as db:
            rows = (
                db.query(
                    models.GoogleOAuthCredential.assistant_id,
                    models.GoogleOAuthCredential.granted_email,
                )
                .all()
            )
            return [(int(r[0]), r[1]) for r in rows]

    try:
        assistants = await asyncio.to_thread(_list_assistants)
    except Exception as exc:  # noqa: BLE001
        return StatusCheck(
            key="google_apis",
            label="Google APIs (Gmail + Calendar)",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary="Could not query google_oauth_credentials table.",
            detail={"error": str(exc)},
        )

    if not assistants:
        return StatusCheck(
            key="google_apis",
            label="Google APIs (Gmail + Calendar)",
            status="unknown",
            latency_ms=round(_now_ms() - started, 1),
            summary="No assistant has connected a Google account yet.",
            detail={},
            hint=(
                "On the Assistant page, click 'Connect with Google' to "
                "enable Avi's Gmail + Calendar autopilot."
            ),
        )

    # Probe each assistant in parallel — Gmail.getProfile +
    # calendarList.list are both ~100 ms calls, so total wall time stays
    # well under our overall status budget.
    per_assistant = await asyncio.gather(
        *[
            asyncio.to_thread(_check_google_for_assistant, aid, email)
            for aid, email in assistants
        ]
    )

    ok_pairs = sum(
        1 for r in per_assistant if r["gmail"] == "ok" and r["calendar"] == "ok"
    )
    any_ok = any(
        r["gmail"] == "ok" or r["calendar"] == "ok" for r in per_assistant
    )
    all_ok = ok_pairs == len(per_assistant)

    if all_ok:
        level: StatusLevel = "ok"
    elif any_ok:
        level = "degraded"
    else:
        level = "down"

    summary = (
        f"{ok_pairs}/{len(per_assistant)} assistant(s) fully connected "
        f"(Gmail + Calendar)"
    )
    hints: list[str] = []
    for r in per_assistant:
        if r["gmail"] != "ok" or r["calendar"] != "ok":
            email = r.get("granted_email") or f"assistant #{r['assistant_id']}"
            if r["gmail"] == "not_connected":
                hints.append(
                    f"{email}: re-connect on the Assistant page → "
                    "'Connect with Google'."
                )
            else:
                hints.append(
                    f"{email}: gmail={r['gmail']} calendar={r['calendar']} "
                    f"({'; '.join(r['errors']) or 'no detail'})"
                )

    return StatusCheck(
        key="google_apis",
        label="Google APIs (Gmail + Calendar)",
        status=level,
        latency_ms=round(_now_ms() - started, 1),
        summary=summary,
        detail={
            "assistants": per_assistant,
            "default_scopes": list(_default_scopes()),
        },
        hint=" · ".join(hints) if hints else None,
    )


def _default_scopes() -> tuple[str, ...]:
    """Lazy import so the orchestrator stays cheap when nothing needs it."""
    from ..integrations import google_oauth

    return google_oauth.DEFAULT_SCOPES


async def check_telegram_api() -> StatusCheck:
    """Verify the Telegram Bot API is reachable and the token is valid.

    Calls ``getMe`` (cheap, free, idempotent) and reports the bot's
    identity. We deliberately do NOT poke ``getUpdates`` from here —
    it would steal the next update from the live inbox poller and
    create exactly one missed message. ``getMe`` is the canonical
    "is the token alive" probe and is what BotFather itself uses.

    The summary surfaces the configured long-poll window so the
    occasional ``Telegram getUpdates failed: transport error: The
    read operation timed out`` warning in the backend log can be
    cross-referenced against it without grepping config: a 25 s
    long-poll riding a 35 s httpx budget naturally races a flaky
    LTE / Wi-Fi link every once in a while, the poller already
    backs off + retries, and that warning is benign as long as
    *this* check stays green.
    """
    settings = get_settings()
    token = settings.TELEGRAM_BOT_TOKEN

    if not token:
        return StatusCheck(
            key="telegram_api",
            label="Telegram Bot API",
            status="unknown",
            summary="TELEGRAM_BOT_TOKEN not set in .env.",
            detail={
                "inbound_enabled": settings.AI_TELEGRAM_INBOUND_ENABLED,
            },
            hint=(
                "Create a bot with @BotFather, copy the token, and add "
                "TELEGRAM_BOT_TOKEN=… to .env. Restart the backend so "
                "the inbox long-poll loop picks it up."
            ),
        )

    if not settings.AI_TELEGRAM_INBOUND_ENABLED:
        return StatusCheck(
            key="telegram_api",
            label="Telegram Bot API",
            status="degraded",
            summary="Token configured but inbound poller is disabled.",
            detail={
                "inbound_enabled": False,
                "longpoll_seconds": settings.AI_TELEGRAM_LONGPOLL_SECONDS,
            },
            hint=(
                "Set AI_TELEGRAM_INBOUND_ENABLED=true in .env to start the "
                "long-poll loop on next backend restart."
            ),
        )

    # Local import keeps the orchestrator import-cheap. The integration
    # module pulls in dataclasses + httpx defaults that nothing else on
    # the status page needs.
    from ..integrations import telegram

    started = _now_ms()
    try:
        identity = await asyncio.wait_for(
            asyncio.to_thread(telegram.get_me, token, timeout_seconds=6.0),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        return StatusCheck(
            key="telegram_api",
            label="Telegram Bot API",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary="getMe timed out after 8 s.",
            detail={
                "longpoll_seconds": settings.AI_TELEGRAM_LONGPOLL_SECONDS,
            },
            hint=(
                "Either the network is blocking api.telegram.org or "
                "Telegram is having an outage — check "
                "https://downdetector.com/status/telegram/."
            ),
        )
    except telegram.TelegramReadError as exc:
        # Distinguish "Telegram says no" (token invalid / revoked /
        # bot deleted) from "we can't reach Telegram at all" (DNS,
        # firewall, captive portal). Both are 'down', but the hints
        # are very different.
        msg = str(exc)
        is_transport = msg.startswith("transport error")
        if is_transport:
            hint = (
                "Verify outbound HTTPS to api.telegram.org works "
                "(`curl -sS https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe`). "
                "DNS, firewall, or captive portal usually."
            )
        elif "401" in msg or "Unauthorized" in msg:
            hint = (
                "Token rejected. Re-issue with @BotFather → /token, "
                "paste the new value into TELEGRAM_BOT_TOKEN in .env, "
                "then restart the backend."
            )
        else:
            hint = (
                "See backend logs for the full error. Common causes: "
                "rate-limit (429), bot deleted, or Telegram outage."
            )
        return StatusCheck(
            key="telegram_api",
            label="Telegram Bot API",
            status="down",
            latency_ms=round(_now_ms() - started, 1),
            summary=f"getMe failed: {msg[:120]}",
            detail={
                "error": msg,
                "longpoll_seconds": settings.AI_TELEGRAM_LONGPOLL_SECONDS,
                "token_suffix": f"…{token[-6:]}" if len(token) > 6 else "(short)",
            },
            hint=hint,
        )

    latency = round(_now_ms() - started, 1)
    handle = f"@{identity.username}" if identity.username else "(no @username set)"
    return StatusCheck(
        key="telegram_api",
        label="Telegram Bot API",
        status="ok",
        latency_ms=latency,
        summary=f"{handle} reachable in {int(latency)} ms",
        detail={
            "bot_user_id": identity.user_id,
            "bot_username": identity.username,
            "bot_first_name": identity.first_name,
            "inbound_enabled": True,
            "longpoll_seconds": settings.AI_TELEGRAM_LONGPOLL_SECONDS,
            "max_per_tick": settings.AI_TELEGRAM_INBOX_MAX_PER_TICK,
            "auto_link_by_phone": settings.AI_TELEGRAM_AUTO_LINK_BY_PHONE,
            "token_suffix": f"…{token[-6:]}" if len(token) > 6 else "(short)",
        },
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


# Order matters: the UI renders rows top-to-bottom in this sequence. We
# group by "infrastructure" first (API, DB, models) then "edges" (ngrok,
# UI) since that mirrors the dependency graph — if Postgres is down,
# nothing above it can possibly work.
_CHECK_REGISTRY: list[tuple[str, str, Callable[[], Awaitable[StatusCheck]]]] = [
    ("api", "FastAPI backend", check_api),
    ("postgres", "Postgres", check_postgres),
    ("ollama_daemon", "Ollama daemon", check_ollama_daemon),
    ("ollama_models", "Ollama models", check_ollama_models),
    ("ai_agent", "AI agent", check_ai_agent),
    ("gemini_api", "Gemini API", check_gemini_api),
    ("google_apis", "Google APIs (Gmail + Calendar)", check_google_apis),
    ("telegram_api", "Telegram Bot API", check_telegram_api),
    ("ngrok_local", "ngrok agent", check_ngrok_local),
    ("ngrok_public", "ngrok public URL", check_ngrok_public),
    ("ui", "React UI", check_ui),
]


def _roll_up(checks: list[StatusCheck]) -> StatusLevel:
    """Combine per-check status into one overall traffic light."""
    if any(c.status == "down" for c in checks):
        return "down"
    if any(c.status == "degraded" for c in checks):
        return "degraded"
    if all(c.status in ("ok", "unknown") for c in checks):
        # If everything we *can* check is happy we call it 'ok' even if
        # one or two rows are 'unknown' (e.g. ngrok not configured).
        if any(c.status == "ok" for c in checks):
            return "ok"
        return "unknown"
    return "unknown"


async def gather_status_report() -> SystemStatusReport:
    """Run every registered check concurrently and collect the results."""
    coros = [
        _safe(key, label, factory) for key, label, factory in _CHECK_REGISTRY
    ]
    results = await asyncio.gather(*coros)
    return SystemStatusReport(
        overall=_roll_up(list(results)),
        generated_at=datetime.now(timezone.utc),
        checks=list(results),
    )
