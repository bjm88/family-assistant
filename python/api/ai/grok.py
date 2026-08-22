"""xAI Grok client (OpenAI-compatible Chat Completions).

Used as Avi's live LLM when :setting:`AI_LLM_PROVIDER` is ``grok``
(the default). Gemini stays reserved for web search; Ollama stays
parked for a future household fine-tune.

We talk to ``https://api.x.ai/v1/chat/completions`` with the same
messages + tools shape the agent loop already uses, so the rest of
the app does not have to know which vendor is behind the call.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from ..config import get_settings
from .ollama import ChatWithToolsResult, ToolCall


logger = logging.getLogger(__name__)


class GrokError(RuntimeError):
    """Anything Grok-related that went wrong."""


class GrokUnavailable(GrokError):
    """Missing API key, auth failure, or xAI unreachable."""


class GrokTimeout(GrokError):
    """Read/connect timeout talking to api.x.ai."""


def _api_key() -> str:
    key = get_settings().grok_api_key
    if not key:
        raise GrokUnavailable(
            "GROK_API_KEY (or XAI_API_KEY) is not set in .env."
        )
    return key


def _base() -> str:
    return get_settings().GROK_API_BASE.rstrip("/")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _normalize_messages(
    messages: List[Dict[str, Any]],
    *,
    system: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Rewrite our mixed Ollama/OpenAI history into Chat Completions form."""
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for i, raw in enumerate(messages):
        role = raw.get("role") or "user"
        msg: Dict[str, Any] = {"role": role}
        content = raw.get("content")
        if content is None:
            content = ""
        msg["content"] = content
        if role == "assistant" and raw.get("tool_calls"):
            calls = []
            for j, c in enumerate(raw["tool_calls"]):
                fn = (c or {}).get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, dict):
                    args = json.dumps(args, default=str)
                elif args is None:
                    args = "{}"
                call_id = (c or {}).get("id") or f"call_{i}_{j}"
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": args,
                        },
                    }
                )
            msg["tool_calls"] = calls
        if role == "tool":
            tool_call_id = raw.get("tool_call_id") or raw.get("id")
            if not tool_call_id:
                # Best-effort: pair with the most recent assistant
                # tool_call of the same name.
                tool_call_id = f"call_{i}"
            msg["tool_call_id"] = tool_call_id
            if raw.get("name"):
                msg["name"] = raw["name"]
        out.append(msg)
    return out


def _raise_for_status(r: httpx.Response, model: str) -> None:
    if r.status_code in (401, 403):
        raise GrokUnavailable(
            f"Grok rejected the API key (HTTP {r.status_code})."
        )
    if r.status_code == 404:
        raise GrokUnavailable(
            f"Grok model {model!r} is not available: {r.text[:300]}"
        )
    if r.status_code >= 400:
        raise GrokError(
            f"Grok returned {r.status_code}: {r.text[:400]}"
        )


async def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
    model: str,
    timeout_seconds: float = 60.0,
) -> str:
    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    call_started = time.monotonic()
    request_timeout = httpx.Timeout(
        connect=10.0, read=timeout_seconds, write=timeout_seconds, pool=5.0
    )
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            r = await client.post(
                f"{_base()}/chat/completions",
                headers=_headers(),
                json=payload,
            )
    except httpx.ConnectError as exc:
        raise GrokUnavailable(f"xAI at {_base()} is not responding: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise GrokTimeout(
            f"Grok {model!r} did not respond within {timeout_seconds:.0f}s "
            f"({type(exc).__name__})."
        ) from exc
    _raise_for_status(r, model)
    data = r.json()
    text_out = _content_from_completion(data)
    logger.info(
        "[grok] generate done model=%s duration_ms=%d "
        "prompt_chars=%d out_chars=%d max_tokens=%d",
        model,
        int((time.monotonic() - call_started) * 1000),
        len(prompt),
        len(text_out),
        max_tokens,
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
) -> ChatWithToolsResult:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages, system=system),
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    logger.info(
        "[grok] chat_with_tools start model=%s msgs=%d sys_chars=%d "
        "tools=%d temp=%.2f timeout=%.0fs",
        model,
        len(messages),
        len(system) if system else 0,
        len(tools),
        temperature,
        timeout_seconds,
    )
    request_timeout = httpx.Timeout(
        connect=10.0, read=timeout_seconds, write=timeout_seconds, pool=5.0
    )
    call_started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            r = await client.post(
                f"{_base()}/chat/completions",
                headers=_headers(),
                json=payload,
            )
    except httpx.ConnectError as exc:
        raise GrokUnavailable(f"xAI at {_base()} is not responding: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise GrokTimeout(
            f"Grok {model!r} did not respond within {timeout_seconds:.0f}s "
            f"({type(exc).__name__})."
        ) from exc
    _raise_for_status(r, model)

    data = r.json()
    choice = ((data.get("choices") or [{}])[0]) or {}
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    calls: List[ToolCall] = []
    for c in message.get("tool_calls") or []:
        fn = (c or {}).get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        if name:
            calls.append(
                ToolCall(
                    name=name,
                    arguments=args,
                    id=(c or {}).get("id") or "",
                )
            )

    logger.info(
        "[grok] chat_with_tools done model=%s duration_ms=%d "
        "tool_calls=%d content_chars=%d",
        model,
        int((time.monotonic() - call_started) * 1000),
        len(calls),
        len(content),
    )
    return ChatWithToolsResult(
        content=content,
        tool_calls=calls,
        model=model,
        raw_message=message,
    )


async def chat_stream(
    messages: List[Dict[str, str]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    model: str,
) -> AsyncIterator[str]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages, system=system),
        "temperature": temperature,
        "stream": True,
    }
    logger.info(
        "[grok] chat_stream start model=%s msgs=%d sys_chars=%d",
        model,
        len(messages),
        len(system) if system else 0,
    )
    call_started = time.monotonic()
    total_chars = 0
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{_base()}/chat/completions",
                headers=_headers(),
                json=payload,
            ) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    raise GrokError(
                        f"Grok returned {r.status_code}: "
                        f"{body.decode(errors='replace')[:400]}"
                    )
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
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
    except httpx.ConnectError as exc:
        raise GrokUnavailable(f"xAI at {_base()} is not responding: {exc}") from exc
    finally:
        logger.info(
            "[grok] chat_stream done model=%s duration_ms=%d out_chars=%d",
            model,
            int((time.monotonic() - call_started) * 1000),
            total_chars,
        )


def _content_from_completion(data: Dict[str, Any]) -> str:
    choice = ((data.get("choices") or [{}])[0]) or {}
    message = choice.get("message") or {}
    return (message.get("content") or "").strip()


async def list_models() -> List[str]:
    """Return model ids the key can see. Used by the status page."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(f"{_base()}/models", headers=_headers())
    _raise_for_status(r, "models")
    data = r.json()
    return [
        str(m.get("id") or "")
        for m in (data.get("data") or [])
        if m.get("id")
    ]


__all__ = [
    "GrokError",
    "GrokTimeout",
    "GrokUnavailable",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "generate",
    "list_models",
]
