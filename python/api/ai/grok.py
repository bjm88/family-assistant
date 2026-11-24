"""xAI Grok backend (OpenAI-compatible Chat Completions).

First in the default :setting:`AI_LLM_PROVIDERS` chain. Talks to
``https://api.x.ai/v1/chat/completions`` with the shared history shape
from :mod:`api.ai.llm_types`. Callers should go through
:mod:`api.ai.llm`, which handles fallback to the next backend.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from ..config import get_settings
from .llm_types import (
    ChatWithToolsResult,
    LLMError,
    LLMModelMissing,
    LLMTimeout,
    LLMUnavailable,
    ToolCall,
    canonical_history,
    parse_arguments,
)


logger = logging.getLogger(__name__)

NAME = "grok"


class GrokError(LLMError):
    """Anything Grok-related that went wrong."""


class GrokUnavailable(GrokError, LLMUnavailable):
    """Missing API key, auth failure, xAI unreachable or overloaded."""


class GrokModelMissing(GrokUnavailable, LLMModelMissing):
    """xAI doesn't know the requested model id."""


class GrokTimeout(GrokError, LLMTimeout):
    """Read timeout talking to api.x.ai."""


# ---------------------------------------------------------------------------
# Backend interface used by api.ai.llm
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    return bool(get_settings().grok_api_key)


def owns_model(tag: str) -> bool:
    return tag.lower().startswith("grok")


def tier_models(tier: str) -> List[str]:
    s = get_settings()
    if tier == "fast":
        return [m for m in (s.GROK_FAST_MODEL or s.GROK_MODEL,) if m]
    return [m for m in (s.GROK_MODEL,) if m]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _api_key() -> str:
    key = get_settings().grok_api_key
    if not key:
        raise GrokUnavailable("GROK_API_KEY (or XAI_API_KEY) is not set in .env.")
    return key


def _base() -> str:
    return get_settings().GROK_API_BASE.rstrip("/")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _timeout(read_s: float) -> httpx.Timeout:
    return httpx.Timeout(connect=5.0, read=read_s, write=read_s, pool=5.0)


def _transport_error(exc: Exception, model: str, timeout_s: float) -> GrokError:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return GrokUnavailable(f"xAI at {_base()} is not reachable: {exc}")
    if isinstance(exc, httpx.TimeoutException):
        return GrokTimeout(
            f"Grok {model!r} did not respond within {timeout_s:.0f}s "
            f"({type(exc).__name__})."
        )
    if isinstance(exc, httpx.TransportError):
        return GrokUnavailable(f"xAI transport error: {exc}")
    return GrokError(str(exc))


def _raise_for_status(status: int, body: str, model: str) -> None:
    if status in (401, 403):
        raise GrokUnavailable(f"Grok rejected the API key (HTTP {status}).")
    if status == 404:
        raise GrokModelMissing(f"Grok model {model!r} is not available: {body[:300]}")
    if status == 429 or status >= 500:
        raise GrokUnavailable(f"Grok returned {status}: {body[:300]}")
    if status >= 400:
        raise GrokError(f"Grok returned {status}: {body[:400]}")


async def _post(payload: Dict[str, Any], model: str, timeout_s: float) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_timeout(timeout_s)) as client:
            r = await client.post(
                f"{_base()}/chat/completions", headers=_headers(), json=payload
            )
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, timeout_s) from exc
    _raise_for_status(r.status_code, r.text, model)
    return r.json()


def _to_wire(messages: List[Dict[str, Any]], system: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in canonical_history(messages):
        msg: Dict[str, Any] = {"role": m["role"], "content": m.get("content") or ""}
        if m["role"] == "assistant" and m.get("tool_calls"):
            msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["function"]["name"],
                        "arguments": json.dumps(c["function"]["arguments"], default=str),
                    },
                }
                for c in m["tool_calls"]
            ]
        if m["role"] == "tool":
            msg["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                msg["name"] = m["name"]
        out.append(msg)
    return out


def _message(data: Dict[str, Any]) -> Dict[str, Any]:
    choice = ((data.get("choices") or [{}])[0]) or {}
    return choice.get("message") or {}


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


async def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
    model: str,
    timeout_seconds: float = 60.0,
) -> str:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _to_wire([{"role": "user", "content": prompt}], system),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    started = time.monotonic()
    data = await _post(payload, model, timeout_seconds)
    text_out = (_message(data).get("content") or "").strip()
    logger.info(
        "[grok] generate done model=%s duration_ms=%d prompt_chars=%d out_chars=%d",
        model,
        int((time.monotonic() - started) * 1000),
        len(prompt),
        len(text_out),
    )
    return text_out


async def chat_oneshot(
    *,
    system: str,
    user: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 8,
    timeout_s: float = 10.0,
) -> str:
    return await generate(
        user,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        timeout_seconds=timeout_s,
    )


async def chat_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    model: str,
    temperature: float = 0.2,
    timeout_seconds: float = 90.0,
    think: Optional[bool] = False,
) -> ChatWithToolsResult:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _to_wire(messages, system),
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    started = time.monotonic()
    data = await _post(payload, model, timeout_seconds)
    message = _message(data)
    content = (message.get("content") or "").strip()
    calls = [
        ToolCall(
            name=fn.get("name") or "",
            arguments=parse_arguments(fn.get("arguments")),
            id=(c or {}).get("id") or "",
        )
        for c in message.get("tool_calls") or []
        for fn in [(c or {}).get("function") or {}]
        if fn.get("name")
    ]
    logger.info(
        "[grok] chat_with_tools done model=%s duration_ms=%d msgs=%d tools=%d "
        "tool_calls=%d content_chars=%d",
        model,
        int((time.monotonic() - started) * 1000),
        len(messages),
        len(tools),
        len(calls),
        len(content),
    )
    return ChatWithToolsResult(
        content=content,
        tool_calls=calls,
        model=model,
        raw_message=message,
        provider=NAME,
    )


async def chat_stream(
    messages: List[Dict[str, Any]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    model: str,
) -> AsyncIterator[str]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _to_wire(messages, system),
        "temperature": temperature,
        "stream": True,
    }
    started = time.monotonic()
    total_chars = 0
    try:
        async with httpx.AsyncClient(timeout=_timeout(120.0)) as client:
            async with client.stream(
                "POST", f"{_base()}/chat/completions", headers=_headers(), json=payload
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    _raise_for_status(r.status_code, body, model)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    delta = ((obj.get("choices") or [{}])[0]).get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        total_chars += len(piece)
                        yield piece
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, 120.0) from exc
    finally:
        logger.info(
            "[grok] chat_stream done model=%s duration_ms=%d out_chars=%d",
            model,
            int((time.monotonic() - started) * 1000),
            total_chars,
        )


async def list_models() -> List[str]:
    """Return model ids the key can see. Used by the status page."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{_base()}/models", headers=_headers())
    except httpx.HTTPError as exc:
        raise _transport_error(exc, "models", 8.0) from exc
    _raise_for_status(r.status_code, r.text, "models")
    return [str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id")]


__all__ = [
    "NAME",
    "GrokError",
    "GrokModelMissing",
    "GrokTimeout",
    "GrokUnavailable",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "generate",
    "is_configured",
    "list_models",
    "owns_model",
    "tier_models",
]
