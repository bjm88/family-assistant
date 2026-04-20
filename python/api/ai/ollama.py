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
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

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


def fast_model() -> str:
    """Lightweight Gemma tag used for structured/planner-style calls.

    Falls back to the main chat model when no fast model is configured
    so callers never have to handle a None case. Operators tier the
    workload by setting :envvar:`AI_OLLAMA_FAST_MODEL` to a small
    model (``gemma4:e2b``) and leaving :envvar:`AI_OLLAMA_MODEL` on
    the heavyweight one (``gemma4:26b``).

    Note: this returns the configured tag even when it isn't actually
    pulled in Ollama. Callers route around the resulting
    :class:`OllamaUnavailable` via :func:`generate_with_fallback` so
    a missing fast model never breaks live chat.
    """
    s = get_settings()
    return s.AI_OLLAMA_FAST_MODEL or s.AI_OLLAMA_MODEL


# Models we've already warned about being unpulled — keeps the log
# clean when the fast tag is intentionally absent (e.g. user is still
# pulling it, or running on a small dev box).
_warned_unavailable: set[str] = set()


async def generate_with_fallback(
    prompt: str,
    *,
    primary_model: str,
    fallback_model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
) -> tuple[str, str]:
    """Try ``primary_model`` and transparently fall back if it's not
    pulled. Returns ``(text, model_actually_used)``.

    Designed for the tiered-model workload where we *prefer* the
    lightweight Gemma but still want the request to succeed against
    the main model when the fast tag isn't installed yet.
    """
    fb = fallback_model or _model()
    try:
        text_value = await generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=primary_model,
        )
        return text_value, primary_model
    except OllamaUnavailable as e:
        if primary_model == fb:
            raise
        if primary_model not in _warned_unavailable:
            _warned_unavailable.add(primary_model)
            logger.warning(
                "Fast model %r is unavailable (%s) — falling back to %r. "
                "Run `ollama pull %s` to enable the tiered path.",
                primary_model,
                e,
                fb,
                primary_model,
            )
        text_value = await generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=fb,
        )
        return text_value, fb


async def warmup_model(
    model: str,
    *,
    keep_alive: str = "1h",
    timeout_seconds: float = 90.0,
) -> bool:
    """Force-load ``model`` into Ollama and pin it in memory.

    Why this exists
    ---------------
    A cold Ollama model takes 1–10 s just to mmap into VRAM before it
    can produce a single token. That cold-start is the dominant cause
    of the *"the live-chat fast-ack didn't fire"* class of bugs:
    `gemma4:e2b` is the lightweight ack model, but if the very first
    chat of the day arrives while e2b is unloaded, the ack call needs
    ~3–4 s just to load the weights — easily exceeding the
    :setting:`AI_FAST_ACK_TIMEOUT_SECONDS` cap and silently dropping
    the ack.

    Sending ``num_predict=0`` with a one-word prompt makes Ollama
    load the model and immediately return without doing real
    inference. Combined with ``keep_alive="1h"`` (vs. the default
    5 min) it stays resident across long quiet periods so the next
    real call is instant.

    Returns ``True`` on success; ``False`` if Ollama is unreachable
    or the model isn't pulled. Never raises — callers (lifespan
    startup) treat warmup as best-effort.
    """
    payload: Dict[str, object] = {
        "model": model,
        "prompt": "ok",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": 1, "temperature": 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            r = await client.post(f"{_base()}/api/generate", json=payload)
    except httpx.ConnectError as exc:
        logger.warning(
            "Ollama warmup: %s is not responding (%s) — skipping warmup",
            _base(),
            exc,
        )
        return False
    except httpx.TimeoutException:
        logger.warning(
            "Ollama warmup: %r exceeded %.0fs while loading — skipping",
            model,
            timeout_seconds,
        )
        return False
    if r.status_code == 404:
        logger.warning(
            "Ollama warmup: model %r is not pulled. Run `ollama pull %s`.",
            model,
            model,
        )
        return False
    if r.status_code >= 400:
        logger.warning(
            "Ollama warmup: %r returned %s: %s",
            model,
            r.status_code,
            r.text[:200],
        )
        return False
    logger.info(
        "Ollama warmup: model %r loaded and pinned for %s", model, keep_alive
    )
    return True


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
    model: Optional[str] = None,
) -> str:
    """One-shot, non-streaming completion.

    ``model`` defaults to :envvar:`AI_OLLAMA_MODEL`; pass a different
    tag (typically the value returned by :func:`fast_model`) to route
    a quick structured-output call to a cheaper model.
    """
    target_model = model or _model()
    payload: Dict[str, object] = {
        "model": target_model,
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
            f"Model '{target_model}' is not pulled. "
            f"Run `ollama pull {target_model}`."
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


# ---------------------------------------------------------------------------
# Tool calling — used by the agent loop (api.ai.agent).
#
# Ollama supports a native ``tools`` parameter on /api/chat for models
# that publish a tool-call template (gemma3, llama3.1+, qwen2.5, …). When
# the model emits a tool call it shows up as ``message.tool_calls`` in
# the response. For models that don't support that channel the assistant
# will instead emit a JSON object inside its prose; :func:`_parse_tool_calls_from_text`
# tries to recover those so we don't completely lose the tool-call path.
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """Single tool invocation requested by the LLM."""

    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatWithToolsResult:
    """Outcome of one round-trip to :func:`chat_with_tools`."""

    content: str  # natural-language portion of the model's reply (often empty when tool_calls are present)
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    raw_message: Dict[str, Any] = field(default_factory=dict)


async def chat_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.2,
    timeout_seconds: float = 90.0,
) -> ChatWithToolsResult:
    """Single non-streaming chat turn with tool-calling enabled.

    ``tools`` follows OpenAI/Ollama function-tool schema:
    ``[{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}, ...]``.

    The agent loop calls this in a tight loop, appending the model's
    ``tool_calls`` and our tool results to ``messages`` between turns.
    Temperature defaults low because tool selection is a structured
    decision; bump it for the final answer if you want more flair.
    """
    target_model = model or _model()
    ollama_messages: List[Dict[str, Any]] = []
    if system:
        ollama_messages.append({"role": "system", "content": system})
    ollama_messages.extend(messages)

    payload: Dict[str, Any] = {
        "model": target_model,
        "messages": ollama_messages,
        "stream": False,
        "tools": tools,
        "options": {"temperature": temperature},
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            r = await client.post(f"{_base()}/api/chat", json=payload)
    except httpx.ConnectError as e:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {e}"
        ) from e

    if r.status_code == 404:
        raise OllamaUnavailable(
            f"Model '{target_model}' is not pulled. "
            f"Run `ollama pull {target_model}`."
        )
    if r.status_code >= 400:
        raise OllamaError(
            f"Ollama returned {r.status_code}: {r.text[:400]}"
        )

    data = r.json()
    message = data.get("message") or {}
    content = (message.get("content") or "").strip()

    # Native tool calls (preferred path when the model template supports it).
    raw_calls = message.get("tool_calls") or []
    calls: List[ToolCall] = []
    for c in raw_calls:
        fn = (c or {}).get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments")
        # Ollama returns ``arguments`` as either a dict or a JSON string
        # depending on the model. Normalize to dict.
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        if name:
            calls.append(ToolCall(name=name, arguments=args))

    # Fallback: model didn't use the tool channel but stuffed a JSON
    # object into the prose. Try to recover. Only used when there were
    # no native tool_calls.
    if not calls and content:
        recovered = _parse_tool_calls_from_text(content, [t["function"]["name"] for t in tools])
        if recovered:
            calls = recovered
            # Strip the JSON tool-call block from the user-visible content.
            content = ""

    return ChatWithToolsResult(
        content=content,
        tool_calls=calls,
        model=target_model,
        raw_message=message,
    )


_TOOL_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"(?:tool|name|function)\"[^{}]*\}", re.DOTALL)


def _parse_tool_calls_from_text(
    text: str, tool_names: List[str]
) -> List[ToolCall]:
    """Best-effort recovery of tool calls from prose-only model replies.

    Some local models won't emit ``message.tool_calls`` even when given
    tools — they instead write something like
    ``{"tool": "gmail_send", "arguments": {...}}`` inline. We scan for
    JSON objects, validate the tool name, and reconstruct
    :class:`ToolCall`s. Returns ``[]`` if nothing parseable is found.
    """
    if not text or not tool_names:
        return []
    candidates: List[str] = []
    # Pull every {...} the model may have written; a balanced parser is
    # overkill — the repair loop below tolerates JSON failures.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(text[start : i + 1])
                start = -1

    out: List[ToolCall] = []
    valid = set(tool_names)
    for blob in candidates:
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Accept several shapes: {tool, arguments} | {name, arguments} | {function: {...}}
        name = obj.get("tool") or obj.get("name")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if not name and isinstance(obj.get("function"), dict):
            name = obj["function"].get("name")
            args = obj["function"].get("arguments", args)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        if name in valid and isinstance(args, dict):
            out.append(ToolCall(name=name, arguments=args))
    return out


def system_prompt_for_avi(assistant_name: str, family_name: Optional[str]) -> str:
    """Personality prompt shared by greet + chat endpoints.

    Persona + non-negotiable response-style rules. The chat UI feeds
    every reply straight into Kokoro TTS, so anything the model writes
    is also what the user hears out loud. Markdown noise (``**bold**``,
    ``*emphasis*``, ``# headings``, ``- bullets``) gets read literally
    as "asterisk asterisk…" and makes Avi sound robotic — so we ban
    those characters here at the source rather than stripping them
    after the fact (which would also mean stripping them out of
    transcripts).
    """
    family_bit = f" the {family_name} family" if family_name else " this family"
    return (
        f"You are {assistant_name}, the friendly live-in AI assistant for"
        f"{family_bit}. You are warm, concise, and genuinely interested "
        f"in the people you talk to. You have access to structured notes "
        f"about each person (name, goals, relationships, residences, "
        f"pets).\n\n"
        "RESPONSE STYLE — your replies are read aloud by a TTS voice "
        "AND shown in a chat bubble. Follow these rules every turn:\n"
        "* Be brief by default — 1 to 3 short sentences. The user can "
        "  ask for more detail; do not pre-emptively dump everything "
        "  you know.\n"
        "* Speak in natural spoken English. Plain prose only.\n"
        "* NEVER use Markdown formatting in your spoken reply — no "
        "  asterisks for bold (**...**), no underscores for italics, "
        "  no leading dashes for bullet lists, no '#' headings, no "
        "  backticks. These get read out loud literally and sound "
        "  awful. If you must enumerate, say 'first', 'second', etc.\n"
        "* For multi-item answers (calendar listings, search results, "
        "  contact lists, summaries with many rows), give the headline "
        "  out loud — the count plus the most important one or two "
        "  items — and then OFFER to email the full details. Example: "
        "  'You've got six events this week. The next one is the "
        "  parent-teacher meeting Tuesday at 4. Want me to email you "
        "  the full list?' Only send the email after the user confirms.\n"
        "* Never invent facts that aren't in the provided context or "
        "  in a tool result you actually called. If you're not sure, "
        "  say so plainly.\n"
        "* Do not sign off ('Let me know if…', 'Hope this helps!') on "
        "  every reply — those add noise when the audio plays back."
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
    "ChatWithToolsResult",
    "OllamaError",
    "OllamaUnavailable",
    "ToolCall",
    "chat_stream",
    "chat_with_tools",
    "fast_model",
    "generate",
    "generate_with_fallback",
    "health",
    "sync_health",
    "system_prompt_for_avi",
    "warmup_model",
]


# Silence an unused-import warning when type-checkers squint.
_ = Iterable
