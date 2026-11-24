"""Local Ollama backend — Avi's offline fallback.

Last in the default :setting:`AI_LLM_PROVIDERS` chain, so it answers
when the cloud backends are unreachable (internet down, xAI outage,
rate limits). Each tier lists a primary and a backup local model:

* heavy: :setting:`AI_OLLAMA_MODEL` (Gemma 4 31B) then
  :setting:`AI_OLLAMA_BACKUP_MODEL` (Meta Muse Glimmer 30B)
* fast: :setting:`AI_OLLAMA_FAST_MODEL` (Gemma 4 E2B) then the heavy model

Callers should go through :mod:`api.ai.llm`, which handles the chain.
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
    public_keys,
)

logger = logging.getLogger(__name__)

NAME = "ollama"


class OllamaError(LLMError):
    """Base for anything Ollama-related that went wrong."""


class OllamaUnavailable(OllamaError, LLMUnavailable):
    """Daemon isn't listening."""


class OllamaModelMissing(OllamaUnavailable, LLMModelMissing):
    """Daemon is up but the model isn't pulled."""


class OllamaTimeout(OllamaError, LLMTimeout):
    """The model accepted the request but didn't answer in time."""


# ---------------------------------------------------------------------------
# Backend interface used by api.ai.llm
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    return bool(get_settings().AI_OLLAMA_MODEL)


def owns_model(tag: str) -> bool:
    low = tag.lower()
    return not (low.startswith("grok") or low.startswith("claude"))


def tier_models(tier: str) -> List[str]:
    s = get_settings()
    if tier == "fast":
        tags = [s.AI_OLLAMA_FAST_MODEL, s.AI_OLLAMA_MODEL, s.AI_OLLAMA_BACKUP_MODEL]
    else:
        tags = [s.AI_OLLAMA_MODEL, s.AI_OLLAMA_BACKUP_MODEL]
    out: List[str] = []
    for t in tags:
        t = (t or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def warm_models() -> List[str]:
    """Models kept resident so a failover answers without a cold load."""
    s = get_settings()
    return [t for t in dict.fromkeys((s.AI_OLLAMA_MODEL, s.AI_OLLAMA_FAST_MODEL)) if t]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _base() -> str:
    return get_settings().AI_OLLAMA_HOST.rstrip("/")


def _timeout(read_s: float) -> httpx.Timeout:
    # Localhost: anything slower than a few seconds to connect means
    # the daemon is dead.
    return httpx.Timeout(connect=5.0, read=read_s, write=read_s, pool=5.0)


def _transport_error(exc: Exception, model: str, timeout_s: float) -> OllamaError:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return OllamaUnavailable(f"Ollama at {_base()} is not responding: {exc}")
    if isinstance(exc, httpx.TimeoutException):
        return OllamaTimeout(
            f"Ollama {model!r} did not respond within {timeout_s:.0f}s "
            f"({type(exc).__name__})."
        )
    return OllamaError(str(exc))


def _raise_for_status(status: int, body: str, model: str) -> None:
    if status == 404:
        raise OllamaModelMissing(f"Model '{model}' is not pulled. Run `ollama pull {model}`.")
    if status >= 400:
        raise OllamaError(f"Ollama returned {status}: {body[:400]}")


async def _post(path: str, payload: Dict[str, Any], model: str, timeout_s: float) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_timeout(timeout_s)) as client:
            r = await client.post(f"{_base()}{path}", json=payload)
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, timeout_s) from exc
    _raise_for_status(r.status_code, r.text, model)
    return r.json()


def _to_wire(messages: List[Dict[str, Any]], system: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in canonical_history(messages):
        msg = public_keys(m)
        if msg["role"] == "tool" and msg.get("name"):
            msg["tool_name"] = msg["name"]
        out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Warmup / health
# ---------------------------------------------------------------------------


async def warmup_model(
    model: str,
    *,
    keep_alive: str = "1h",
    timeout_seconds: float = 120.0,
) -> bool:
    """Force-load ``model`` into memory and pin it for ``keep_alive``.

    A cold model takes seconds to load before its first token, which
    would blow the fast-ack budget and make the first offline reply
    slow. ``num_predict=1`` loads the weights and returns immediately.
    Returns ``False`` (never raises) when Ollama is down or the model
    isn't pulled.
    """
    payload: Dict[str, object] = {
        "model": model,
        "prompt": "ok",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": 1, "temperature": 0.0},
    }
    try:
        await _post("/api/generate", payload, model, timeout_seconds)
    except OllamaError as exc:
        logger.warning("Ollama warmup of %r skipped: %s", model, exc)
        return False
    logger.info("Ollama warmup: model %r loaded and pinned for %s", model, keep_alive)
    return True


async def installed_models() -> List[str]:
    """Tags pulled into the local daemon. Raises OllamaUnavailable if down."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{_base()}/api/tags")
    except httpx.HTTPError as exc:
        raise _transport_error(exc, "tags", 4.0) from exc
    _raise_for_status(r.status_code, r.text, "tags")
    return [str(m.get("name")) for m in (r.json().get("models") or []) if m.get("name")]


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
    """One-shot completion with thinking off so the budget goes to the answer."""
    return await chat_oneshot(
        system=system or "",
        user=prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_s=timeout_seconds,
    )


async def chat_oneshot(
    *,
    system: str,
    user: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 8,
    timeout_s: float = 10.0,
) -> str:
    """Non-streaming ``/api/chat`` used by classifiers, acks and planners.

    ``think=False`` stops Gemma 4 from spending the whole token budget
    on its hidden thinking channel.
    """
    messages = [{"role": "user", "content": user}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": "1h",
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    started = time.monotonic()
    data = await _post("/api/chat", payload, model, timeout_s)
    text_out = ((data.get("message") or {}).get("content") or "").strip()
    logger.info(
        "[ollama] oneshot done model=%s duration_ms=%d out_chars=%d",
        model,
        int((time.monotonic() - started) * 1000),
        len(text_out),
    )
    return text_out


async def chat_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    model: str,
    temperature: float = 0.2,
    timeout_seconds: float = 90.0,
    think: Optional[bool] = False,
    keep_alive: str = "1h",
    num_ctx: int = 32768,
    top_k: int = 40,
    top_p: float = 0.9,
) -> ChatWithToolsResult:
    """Single non-streaming chat turn with tool calling.

    ``think`` defaults to ``False`` because Gemma 4 thinks by default
    and can spend its whole budget on the hidden channel, returning no
    content and no tool calls; the monitoring scheduler opts in.

    Hot-cache options: ``keep_alive`` keeps the model resident between
    agent cycles; a fixed ``num_ctx`` stops Ollama re-sizing (and
    rebuilding) the KV cache whenever the prompt length changes, with
    32K leaving room for the inbound prompt plus tool results; tighter
    ``top_k`` / ``top_p`` make tool picks more repeatable.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _to_wire(messages, system),
        "stream": False,
        "tools": tools,
        "keep_alive": keep_alive,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "top_k": top_k,
            "top_p": top_p,
        },
    }
    if think is not None:
        payload["think"] = bool(think)

    started = time.monotonic()
    data = await _post("/api/chat", payload, model, timeout_seconds)
    message = data.get("message") or {}
    content = (message.get("content") or "").strip()
    calls = [
        ToolCall(name=fn["name"], arguments=parse_arguments(fn.get("arguments")))
        for c in message.get("tool_calls") or []
        for fn in [(c or {}).get("function") or {}]
        if fn.get("name")
    ]
    # Some local models write the call as JSON in prose instead of using
    # the tool channel; recover it so the agent loop still acts.
    if not calls and content:
        recovered = _parse_tool_calls_from_text(content, [t["function"]["name"] for t in tools])
        if recovered:
            calls = recovered
            content = ""

    logger.info(
        "[ollama] chat_with_tools done model=%s duration_ms=%d msgs=%d tools=%d "
        "think=%s tool_calls=%d content_chars=%d prompt_tokens=%s out_tokens=%s",
        model,
        int((time.monotonic() - started) * 1000),
        len(messages),
        len(tools),
        think,
        len(calls),
        len(content),
        data.get("prompt_eval_count"),
        data.get("eval_count"),
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
    """Stream a chat response token-by-token. Yields plain text chunks."""
    payload = {
        "model": model,
        "messages": [m for m in _to_wire(messages, system) if m.get("content")],
        "stream": True,
        "think": False,
        "keep_alive": "1h",
        "options": {"temperature": temperature},
    }
    started = time.monotonic()
    total_chars = 0
    try:
        async with httpx.AsyncClient(timeout=_timeout(120.0)) as client:
            async with client.stream("POST", f"{_base()}/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    _raise_for_status(r.status_code, body, model)
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = (obj.get("message") or {}).get("content") or ""
                    if chunk:
                        total_chars += len(chunk)
                        yield chunk
                    if obj.get("done"):
                        break
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, 120.0) from exc
    finally:
        logger.info(
            "[ollama] chat_stream done model=%s duration_ms=%d out_chars=%d",
            model,
            int((time.monotonic() - started) * 1000),
            total_chars,
        )


def _parse_tool_calls_from_text(text: str, tool_names: List[str]) -> List[ToolCall]:
    """Best-effort recovery of tool calls written as JSON in the prose.

    Accepts ``{tool|name, arguments|args|parameters}`` and
    ``{function: {name, arguments}}`` shapes; ignores unknown tool names.
    """
    if not text or not tool_names:
        return []
    candidates: List[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(text[start : i + 1])
                start = -1

    valid = set(tool_names)
    out: List[ToolCall] = []
    for blob in candidates:
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if not name and isinstance(obj.get("function"), dict):
            name = obj["function"].get("name")
            args = obj["function"].get("arguments", args)
        if name in valid:
            out.append(ToolCall(name=name, arguments=parse_arguments(args)))
    return out


__all__ = [
    "NAME",
    "OllamaError",
    "OllamaModelMissing",
    "OllamaTimeout",
    "OllamaUnavailable",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "generate",
    "installed_models",
    "is_configured",
    "owns_model",
    "tier_models",
    "warm_models",
    "warmup_model",
]
