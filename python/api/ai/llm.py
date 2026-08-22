"""Provider-agnostic LLM facade.

Callers (agent loop, triage, family Q&A, planner, fast-ack) import
from this module instead of talking to Ollama or Grok directly.

``AI_LLM_PROVIDER`` selects the backend:

* ``grok`` (default) — xAI Chat Completions. Used for every LLM task
  except web search.
* ``ollama`` — local daemon. Parked until a household fine-tune is
  ready; flip the env var to re-enable.

Gemini is intentionally *not* a provider here. Web search stays on
Gemini via :mod:`api.integrations.web_search`.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from ..config import get_settings
from . import grok, ollama
from .ollama import ChatWithToolsResult, ToolCall, system_prompt_for_avi


logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Base for anything the live LLM backend raised."""


class LLMUnavailable(LLMError):
    """Key missing, daemon down, or model not available."""


class LLMTimeout(LLMError):
    """Backend accepted the request but did not answer in time."""


def provider() -> str:
    return get_settings().llm_provider


def _looks_like_grok(tag: str) -> bool:
    return tag.lower().startswith("grok")


def _model() -> str:
    settings = get_settings()
    if provider() == "ollama":
        return settings.AI_OLLAMA_MODEL
    return settings.GROK_MODEL


def fast_model() -> str:
    settings = get_settings()
    if provider() == "ollama":
        return settings.AI_OLLAMA_FAST_MODEL or settings.AI_OLLAMA_MODEL
    return settings.GROK_FAST_MODEL or settings.GROK_MODEL


def resolve_model(override: Optional[str] = None, *, fast: bool = False) -> str:
    """Pick a model tag, ignoring leftover Ollama tags when Grok is live.

    ``AI_FAMILY_QA_MODEL=llama3.2:3b`` (or any other local tag) is
    still sitting in some .env files from the fine-tune pipeline.
    Sending that string to api.x.ai 404s, so we drop any override
    that doesn't match the active provider.
    """
    tag = (override or "").strip()
    fallback = fast_model() if fast else _model()
    if not tag:
        return fallback
    if provider() == "grok" and not _looks_like_grok(tag):
        return fallback
    if provider() == "ollama" and _looks_like_grok(tag):
        return fallback
    return tag


def ollama_warmup_enabled() -> bool:
    """True only when local inference is actually in use."""
    settings = get_settings()
    return provider() == "ollama" or bool(settings.AI_OLLAMA_ENABLED)


def _wrap_ollama(exc: BaseException) -> LLMError:
    if isinstance(exc, ollama.OllamaTimeout):
        return LLMTimeout(str(exc))
    if isinstance(exc, ollama.OllamaUnavailable):
        return LLMUnavailable(str(exc))
    if isinstance(exc, ollama.OllamaError):
        return LLMError(str(exc))
    return LLMError(str(exc))


def _wrap_grok(exc: BaseException) -> LLMError:
    if isinstance(exc, grok.GrokTimeout):
        return LLMTimeout(str(exc))
    if isinstance(exc, grok.GrokUnavailable):
        return LLMUnavailable(str(exc))
    if isinstance(exc, grok.GrokError):
        return LLMError(str(exc))
    return LLMError(str(exc))


async def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
    model: Optional[str] = None,
) -> str:
    target = resolve_model(model)
    try:
        if provider() == "ollama":
            return await ollama.generate(
                prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                model=target,
            )
        return await grok.generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=target,
        )
    except (ollama.OllamaError, grok.GrokError) as exc:
        raise (_wrap_ollama(exc) if isinstance(exc, ollama.OllamaError) else _wrap_grok(exc)) from exc


async def generate_with_fallback(
    prompt: str,
    *,
    primary_model: str,
    fallback_model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
) -> tuple[str, str]:
    primary = resolve_model(primary_model, fast=True)
    fallback = resolve_model(fallback_model) if fallback_model else _model()
    try:
        text = await generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=primary,
        )
        return text, primary
    except LLMUnavailable:
        if primary == fallback:
            raise
        logger.warning(
            "Primary model %r unavailable — falling back to %r",
            primary,
            fallback,
        )
        text = await generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            model=fallback,
        )
        return text, fallback


async def chat_oneshot(
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 8,
    timeout_s: float = 10.0,
) -> str:
    target = resolve_model(model, fast=True)
    try:
        if provider() == "ollama":
            return await ollama.chat_oneshot(
                system=system,
                user=user,
                model=target,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
            )
        return await grok.chat_oneshot(
            system=system,
            user=user,
            model=target,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
    except (ollama.OllamaError, grok.GrokError) as exc:
        raise (_wrap_ollama(exc) if isinstance(exc, ollama.OllamaError) else _wrap_grok(exc)) from exc


async def chat_with_tools(
    messages: List[Dict[str, Any]],
    *,
    tools: List[Dict[str, Any]],
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.2,
    timeout_seconds: float = 90.0,
    think: Optional[bool] = False,
    **_ignored: Any,
) -> ChatWithToolsResult:
    target = resolve_model(model)
    try:
        if provider() == "ollama":
            return await ollama.chat_with_tools(
                messages,
                tools=tools,
                system=system,
                model=target,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
                think=think,
            )
        return await grok.chat_with_tools(
            messages,
            tools=tools,
            system=system,
            model=target,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except (ollama.OllamaError, grok.GrokError) as exc:
        raise (_wrap_ollama(exc) if isinstance(exc, ollama.OllamaError) else _wrap_grok(exc)) from exc


async def chat_stream(
    messages: List[Dict[str, str]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    if provider() == "ollama":
        async for chunk in ollama.chat_stream(
            messages, system=system, temperature=temperature
        ):
            yield chunk
        return
    async for chunk in grok.chat_stream(
        messages,
        system=system,
        temperature=temperature,
        model=_model(),
    ):
        yield chunk


__all__ = [
    "ChatWithToolsResult",
    "LLMError",
    "LLMTimeout",
    "LLMUnavailable",
    "ToolCall",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "fast_model",
    "generate",
    "generate_with_fallback",
    "ollama_warmup_enabled",
    "provider",
    "resolve_model",
    "system_prompt_for_avi",
    "_model",
]
