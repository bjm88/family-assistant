"""Thin HTTP client for a local Ollama daemon.

Only implements the two calls we actually need:

* ``generate()``  — one-shot completion used for the opening greeting.
* ``chat_stream()`` — streaming SSE-style conversation used by the chat
  endpoint.

Both calls accept a plain dict prompt and yield text chunks; we keep the
surface area intentionally tiny so the rest of the app doesn't have to
care that Ollama is behind it.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Dict, Iterable, List, Optional

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Base for anything Ollama-related that went wrong."""


class OllamaUnavailable(OllamaError):
    """Daemon isn't listening or the requested model isn't pulled."""


def _base() -> str:
    return get_settings().AI_OLLAMA_HOST.rstrip("/")


def _model() -> str:
    return get_settings().AI_OLLAMA_MODEL


async def health() -> Dict[str, object]:
    """Report whether Ollama is reachable and whether the model is pulled."""
    out: Dict[str, object] = {
        "host": _base(),
        "model": _model(),
        "available": False,
        "model_pulled": False,
        "installed_models": [],
    }
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{_base()}/api/tags")
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out

    out["available"] = True
    names = [m.get("name", "") for m in data.get("models", [])]
    out["installed_models"] = names
    # Ollama model names can include tags; match on the bare name too.
    want = _model()
    out["model_pulled"] = any(
        n == want or n.split(":")[0] == want.split(":")[0] for n in names
    )
    return out


async def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
) -> str:
    """One-shot, non-streaming completion. Used for greetings."""
    payload: Dict[str, object] = {
        "model": _model(),
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if system:
        payload["system"] = system

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{_base()}/api/generate", json=payload)
    except httpx.ConnectError as e:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {e}"
        ) from e

    if r.status_code == 404:
        raise OllamaUnavailable(
            f"Model '{_model()}' is not pulled. Run `ollama pull {_model()}`."
        )
    if r.status_code >= 400:
        raise OllamaError(
            f"Ollama returned {r.status_code}: {r.text[:400]}"
        )
    data = r.json()
    return (data.get("response") or "").strip()


async def chat_stream(
    messages: List[Dict[str, str]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """Stream a chat response token-by-token. Yields plain text chunks."""
    ollama_messages: List[Dict[str, str]] = []
    if system:
        ollama_messages.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not content:
            continue
        ollama_messages.append({"role": role, "content": content})

    payload = {
        "model": _model(),
        "messages": ollama_messages,
        "stream": True,
        "options": {"temperature": temperature},
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{_base()}/api/chat", json=payload
            ) as r:
                if r.status_code == 404:
                    raise OllamaUnavailable(
                        f"Model '{_model()}' is not pulled. "
                        f"Run `ollama pull {_model()}`."
                    )
                if r.status_code >= 400:
                    body = await r.aread()
                    raise OllamaError(
                        f"Ollama returned {r.status_code}: "
                        f"{body.decode(errors='replace')[:400]}"
                    )
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message") or {}
                    chunk = msg.get("content") or ""
                    if chunk:
                        yield chunk
                    if obj.get("done"):
                        break
    except httpx.ConnectError as e:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {e}"
        ) from e


def system_prompt_for_avi(assistant_name: str, family_name: Optional[str]) -> str:
    """Personality prompt shared by greet + chat endpoints."""
    family_bit = f" the {family_name} family" if family_name else " this family"
    return (
        f"You are {assistant_name}, the friendly live-in AI assistant for"
        f"{family_bit}. You are warm, concise, and genuinely interested "
        f"in the people you talk to. You have access to structured notes "
        f"about each person (name, goals, relationships, residences, "
        f"pets). Keep responses short (1–3 sentences) unless the user "
        f"specifically asks for more detail. Never invent facts that "
        f"aren't in the provided context; if you're not sure, say so "
        f"plainly. Speak in natural spoken English — this will often be "
        f"read aloud."
    )


# Tiny helper used by tests / the status endpoint.
def sync_health() -> Dict[str, object]:
    """Blocking variant of ``health()`` for places that aren't async."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Shouldn't happen — we're only calling this from sync code.
            raise RuntimeError("sync_health called from running loop")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(health())


__all__ = [
    "OllamaError",
    "OllamaUnavailable",
    "chat_stream",
    "generate",
    "health",
    "sync_health",
    "system_prompt_for_avi",
]


# Silence an unused-import warning when type-checkers squint.
_ = Iterable
