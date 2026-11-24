"""Anthropic Claude backend (native Messages API over httpx).

Second in the default :setting:`AI_LLM_PROVIDERS` chain. Skipped
automatically while ``ANTHROPIC_API_KEY`` is unset or still the
``sk-ant-xxx`` placeholder.

Claude 5.x models reject ``temperature`` / ``top_p`` / ``top_k`` and
count adaptive thinking against ``max_tokens``, so short calls turn
thinking off where the model allows it and lower ``effort`` instead of
sampling knobs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

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
)


logger = logging.getLogger(__name__)

NAME = "claude"
API_VERSION = "2023-06-01"
# Thinking tokens count toward max_tokens on models where it can't be
# switched off; leave room for it on short calls.
_THINKING_HEADROOM = 4096
_TOOL_TURN_MAX_TOKENS = 16000
_STREAM_MAX_TOKENS = 4096


class ClaudeError(LLMError):
    """Anything Claude-related that went wrong."""


class ClaudeUnavailable(ClaudeError, LLMUnavailable):
    """Missing/placeholder key, auth failure, unreachable or overloaded."""


class ClaudeModelMissing(ClaudeUnavailable, LLMModelMissing):
    """Anthropic doesn't know the requested model id."""


class ClaudeTimeout(ClaudeError, LLMTimeout):
    """Read timeout talking to api.anthropic.com."""


# ---------------------------------------------------------------------------
# Backend interface used by api.ai.llm
# ---------------------------------------------------------------------------


def _key() -> Optional[str]:
    key = (get_settings().ANTHROPIC_API_KEY or "").strip()
    if not key or "xxx" in key.lower() or key.endswith("..."):
        return None
    return key


def is_configured() -> bool:
    return _key() is not None


def owns_model(tag: str) -> bool:
    return tag.lower().startswith("claude")


def tier_models(tier: str) -> List[str]:
    s = get_settings()
    if tier == "fast":
        return [m for m in (s.CLAUDE_FAST_MODEL or s.CLAUDE_MODEL,) if m]
    return [m for m in (s.CLAUDE_MODEL,) if m]


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _base() -> str:
    return get_settings().CLAUDE_API_BASE.rstrip("/")


def _headers() -> Dict[str, str]:
    key = _key()
    if key is None:
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set in .env.")
    return {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }


def _timeout(read_s: float) -> httpx.Timeout:
    return httpx.Timeout(connect=5.0, read=read_s, write=read_s, pool=5.0)


def _transport_error(exc: Exception, model: str, timeout_s: float) -> ClaudeError:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ClaudeUnavailable(f"Anthropic at {_base()} is not reachable: {exc}")
    if isinstance(exc, httpx.TimeoutException):
        return ClaudeTimeout(
            f"Claude {model!r} did not respond within {timeout_s:.0f}s "
            f"({type(exc).__name__})."
        )
    if isinstance(exc, httpx.TransportError):
        return ClaudeUnavailable(f"Anthropic transport error: {exc}")
    return ClaudeError(str(exc))


def _raise_for_status(status: int, body: str, model: str) -> None:
    if status in (401, 403):
        raise ClaudeUnavailable(f"Claude rejected the API key (HTTP {status}).")
    if status == 404:
        raise ClaudeModelMissing(f"Claude model {model!r} is not available: {body[:300]}")
    if status == 429 or status >= 500:
        raise ClaudeUnavailable(f"Claude returned {status}: {body[:300]}")
    if status >= 400:
        raise ClaudeError(f"Claude returned {status}: {body[:400]}")


async def _post(payload: Dict[str, Any], model: str, timeout_s: float) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_timeout(timeout_s)) as client:
            r = await client.post(f"{_base()}/messages", headers=_headers(), json=payload)
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, timeout_s) from exc
    _raise_for_status(r.status_code, r.text, model)
    return r.json()


def _short_call_options(model: str, max_tokens: int) -> Dict[str, Any]:
    """Thinking/effort settings for small non-tool calls."""
    name = model.lower()
    opts: Dict[str, Any] = {"output_config": {"effort": "low"}}
    if "haiku" in name:
        opts["thinking"] = {"type": "disabled"}
        opts["max_tokens"] = max(max_tokens, 16)
    elif "sonnet" in name:
        # Sonnet rejects "disabled"; "between_tools" skips up-front thinking.
        opts["thinking"] = {"type": "between_tools"}
        opts["max_tokens"] = max(max_tokens, 16)
    else:
        opts["max_tokens"] = max_tokens + _THINKING_HEADROOM
    return opts


# ---------------------------------------------------------------------------
# History conversion
# ---------------------------------------------------------------------------

_ID_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _tool_id(raw: str) -> str:
    return _ID_BAD_CHARS.sub("_", raw)[:64] or "call"


def _blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    text = str(content or "")
    return [{"type": "text", "text": text}] if text.strip() else []


def _to_wire(
    messages: List[Dict[str, Any]], system: Optional[str]
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    system_parts = [system] if system else []
    turns: List[Dict[str, Any]] = []

    def push(role: str, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    for m in canonical_history(messages):
        role = m["role"]
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
        elif role == "user":
            push("user", _blocks(m.get("content")))
        elif role == "assistant":
            if m.get("_claude_content"):
                push("assistant", list(m["_claude_content"]))
                continue
            blocks = _blocks(m.get("content"))
            for c in m.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": _tool_id(c["id"]),
                        "name": c["function"]["name"],
                        "input": c["function"]["arguments"],
                    }
                )
            push("assistant", blocks)
        elif role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": _tool_id(m["tool_call_id"]),
                        "content": str(m.get("content") or ""),
                    }
                ],
            )

    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continuing)"}]})
    return ("\n\n".join(system_parts) or None), turns


def _tools_to_wire(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in tools:
        fn = t.get("function") or t
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _text_of(blocks: List[Dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


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
    sys_text, turns = _to_wire([{"role": "user", "content": prompt}], system)
    payload: Dict[str, Any] = {"model": model, "messages": turns}
    payload.update(_short_call_options(model, max_tokens))
    if sys_text:
        payload["system"] = sys_text
    started = time.monotonic()
    data = await _post(payload, model, timeout_seconds)
    text_out = _text_of(data.get("content") or [])
    logger.info(
        "[claude] generate done model=%s duration_ms=%d prompt_chars=%d out_chars=%d",
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
    sys_text, turns = _to_wire(messages, system)
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": _TOOL_TURN_MAX_TOKENS,
        "messages": turns,
        "output_config": {"effort": get_settings().CLAUDE_EFFORT},
    }
    if sys_text:
        payload["system"] = sys_text
    if tools:
        payload["tools"] = _tools_to_wire(tools)

    started = time.monotonic()
    data = await _post(payload, model, timeout_seconds)
    blocks = data.get("content") or []
    calls = [
        ToolCall(name=b["name"], arguments=b.get("input") or {}, id=b.get("id") or "")
        for b in blocks
        if b.get("type") == "tool_use" and b.get("name")
    ]
    content = _text_of(blocks)
    logger.info(
        "[claude] chat_with_tools done model=%s duration_ms=%d msgs=%d tools=%d "
        "tool_calls=%d content_chars=%d stop=%s",
        model,
        int((time.monotonic() - started) * 1000),
        len(messages),
        len(tools),
        len(calls),
        len(content),
        data.get("stop_reason"),
    )
    return ChatWithToolsResult(
        content=content,
        tool_calls=calls,
        model=model,
        raw_message={"role": "assistant", "content": blocks},
        provider=NAME,
        # Thinking blocks must be echoed back with the tool results.
        history_extras={"_claude_content": blocks} if calls else {},
    )


async def chat_stream(
    messages: List[Dict[str, Any]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    model: str,
) -> AsyncIterator[str]:
    sys_text, turns = _to_wire(messages, system)
    payload: Dict[str, Any] = {"model": model, "messages": turns, "stream": True}
    payload.update(_short_call_options(model, _STREAM_MAX_TOKENS))
    if sys_text:
        payload["system"] = sys_text
    started = time.monotonic()
    total_chars = 0
    try:
        async with httpx.AsyncClient(timeout=_timeout(120.0)) as client:
            async with client.stream(
                "POST", f"{_base()}/messages", headers=_headers(), json=payload
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    _raise_for_status(r.status_code, body, model)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = obj.get("type")
                    if kind == "error":
                        err = obj.get("error") or {}
                        raise ClaudeUnavailable(
                            f"Claude stream error: {err.get('type')}: {err.get('message')}"
                        )
                    if kind == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            total_chars += len(delta["text"])
                            yield delta["text"]
                    elif kind == "message_stop":
                        break
    except httpx.HTTPError as exc:
        raise _transport_error(exc, model, 120.0) from exc
    finally:
        logger.info(
            "[claude] chat_stream done model=%s duration_ms=%d out_chars=%d",
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
    "ClaudeError",
    "ClaudeModelMissing",
    "ClaudeTimeout",
    "ClaudeUnavailable",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "generate",
    "is_configured",
    "list_models",
    "owns_model",
    "tier_models",
]
