"""The one LLM adapter: every model call in the app goes through here.

:setting:`AI_LLM_PROVIDERS` is an ordered chain (default
``grok,claude,ollama``). Each call tries the first provider and falls
through to the next when it fails, so Avi keeps answering on the local
Ollama models when the internet (or a cloud vendor) is down:

* Unconfigured providers (no key, placeholder key) are skipped.
* A provider that is unreachable, rejects its key, or is overloaded is
  marked down for :setting:`AI_LLM_FAILOVER_COOLDOWN_S` so later calls
  go straight to the next one instead of each paying a connect timeout.
  If every provider is marked down they are all tried anyway.
* A missing model only skips that model; the provider's next model for
  the tier is tried (e.g. Gemma 4 31B, then Muse Glimmer).
* A timeout or a bad-request error moves on to the next provider
  without marking the failing one down.

Calls come in two tiers. ``heavy`` (agent loop, streaming chat,
greetings) and ``fast`` (classifiers, acks, planners, family Q&A); each
backend maps a tier to its own models via ``tier_models``. A ``model``
override is honoured only by the backend that owns that tag — a Grok
id is never sent to Ollama and vice versa — and otherwise just selects
the tier.

Backends (:mod:`.grok`, :mod:`.claude`, :mod:`.ollama`) share the types
in :mod:`.llm_types` and raise its error classes, so callers only ever
catch :class:`LLMError` and friends from this module.
"""

from __future__ import annotations

import logging
import time
from types import ModuleType
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

from ..config import get_settings
from . import claude, grok, ollama
from .llm_types import (
    ChatWithToolsResult,
    LLMError,
    LLMModelMissing,
    LLMTimeout,
    LLMUnavailable,
    ToolCall,
    new_call_id,
)


logger = logging.getLogger(__name__)

T = TypeVar("T")

HEAVY = "heavy"
FAST = "fast"

BACKENDS: Dict[str, ModuleType] = {
    grok.NAME: grok,
    claude.NAME: claude,
    ollama.NAME: ollama,
}

# Backend name -> monotonic time until which it is considered down.
_down_until: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# Chain + health bookkeeping
# ---------------------------------------------------------------------------


def chain() -> List[ModuleType]:
    """Configured backends in priority order."""
    out: List[ModuleType] = []
    for raw in (get_settings().AI_LLM_PROVIDERS or "").split(","):
        backend = BACKENDS.get(raw.strip().lower())
        if backend is not None and backend not in out and backend.is_configured():
            out.append(backend)
    return out or [ollama]


def _attempt_order() -> List[ModuleType]:
    now = time.monotonic()
    configured = chain()
    up = [b for b in configured if _down_until.get(b.NAME, 0.0) <= now]
    return up or configured


def _mark_down(backend: ModuleType, exc: BaseException) -> None:
    cooldown = max(0.0, float(get_settings().AI_LLM_FAILOVER_COOLDOWN_S))
    was_up = _down_until.get(backend.NAME, 0.0) <= time.monotonic()
    _down_until[backend.NAME] = time.monotonic() + cooldown
    if was_up:
        logger.warning(
            "[llm] %s marked down for %.0fs: %s", backend.NAME, cooldown, exc
        )


def _mark_up(backend: ModuleType) -> None:
    if _down_until.pop(backend.NAME, None) is not None:
        logger.info("[llm] %s is answering again", backend.NAME)


def reset_health() -> None:
    """Forget every down mark (tests, manual retry from the status page)."""
    _down_until.clear()


def backend_states() -> List[Dict[str, Any]]:
    """Chain snapshot for the status page."""
    now = time.monotonic()
    active = chain()
    names = [n.strip().lower() for n in (get_settings().AI_LLM_PROVIDERS or "").split(",") if n.strip()]
    out = []
    for name in dict.fromkeys(names):
        backend = BACKENDS.get(name)
        if backend is None:
            continue
        down_for = _down_until.get(name, 0.0) - now
        out.append(
            {
                "name": name,
                "configured": backend.is_configured(),
                "in_chain": backend in active,
                "down_for_s": round(down_for, 1) if down_for > 0 else 0.0,
                "heavy_models": backend.tier_models(HEAVY),
                "fast_models": backend.tier_models(FAST),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------


def _tier_of(override: Optional[str], default: str) -> str:
    tag = (override or "").strip()
    if not tag:
        return default
    for backend in BACKENDS.values():
        fast = backend.tier_models(FAST)
        heavy = backend.tier_models(HEAVY)
        if fast and tag == fast[0]:
            return FAST
        if heavy and tag == heavy[0]:
            return HEAVY
    return default


def _models_for(backend: ModuleType, tier: str, override: Optional[str]) -> List[str]:
    tier_list = backend.tier_models(tier)
    tag = (override or "").strip()
    if tag and backend.owns_model(tag):
        return [tag] + [m for m in tier_list if m != tag]
    return tier_list


def provider() -> str:
    """Name of the backend that will be tried first right now."""
    return _attempt_order()[0].NAME


def resolve_model(override: Optional[str] = None, *, fast: bool = False) -> str:
    """The model the first backend would use for this call (for logs/UI)."""
    tier = _tier_of(override, FAST if fast else HEAVY)
    models = _models_for(_attempt_order()[0], tier, override)
    return models[0] if models else (override or "")


def _model() -> str:
    return resolve_model()


def fast_model() -> str:
    return resolve_model(fast=True)


def ollama_warmup_enabled() -> bool:
    """True when the local models are part of the chain and should stay warm."""
    return ollama in chain()


# ---------------------------------------------------------------------------
# Failover core
# ---------------------------------------------------------------------------


def _combined_error(op: str, errors: List[Tuple[str, str, LLMError]]) -> LLMError:
    detail = "; ".join(f"{name}/{model}: {exc}" for name, model, exc in errors)
    message = f"All LLM providers failed for {op}: {detail}"
    excs = [e for _, _, e in errors]
    if excs and all(isinstance(e, LLMTimeout) for e in excs):
        return LLMTimeout(message)
    if excs and all(isinstance(e, LLMUnavailable) for e in excs):
        return LLMUnavailable(message)
    return LLMError(message)


async def _call(
    op: str,
    tier: str,
    override: Optional[str],
    fn: Callable[[ModuleType, str], Awaitable[T]],
) -> Tuple[T, ModuleType, str]:
    errors: List[Tuple[str, str, LLMError]] = []
    for backend in _attempt_order():
        for model in _models_for(backend, tier, override):
            try:
                result = await fn(backend, model)
            except LLMModelMissing as exc:
                errors.append((backend.NAME, model, exc))
                continue
            except LLMUnavailable as exc:
                errors.append((backend.NAME, model, exc))
                _mark_down(backend, exc)
                break
            except LLMError as exc:
                errors.append((backend.NAME, model, exc))
                break
            _mark_up(backend)
            if errors:
                logger.warning(
                    "[llm] %s answered by fallback %s/%s after: %s",
                    op,
                    backend.NAME,
                    model,
                    "; ".join(f"{n}/{m}: {type(e).__name__}" for n, m, e in errors),
                )
            return result, backend, model
    raise _combined_error(op, errors)


# ---------------------------------------------------------------------------
# Public calls
# ---------------------------------------------------------------------------


async def generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
    model: Optional[str] = None,
) -> str:
    text, _, _ = await _call(
        "generate",
        _tier_of(model, HEAVY),
        model,
        lambda b, m: b.generate(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens, model=m
        ),
    )
    return text


async def generate_with_fallback(
    prompt: str,
    *,
    primary_model: str,
    fallback_model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 300,
) -> Tuple[str, str]:
    """Fast-tier generate that also reports which model answered.

    ``fallback_model`` is accepted for older callers; the provider chain
    already falls back across models and backends.
    """
    text, _, used = await _call(
        "generate",
        _tier_of(primary_model, FAST),
        primary_model,
        lambda b, m: b.generate(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens, model=m
        ),
    )
    return text, used


async def chat_oneshot(
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 8,
    timeout_s: float = 10.0,
) -> str:
    text, _, _ = await _call(
        "chat_oneshot",
        _tier_of(model, FAST),
        model,
        lambda b, m: b.chat_oneshot(
            system=system,
            user=user,
            model=m,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        ),
    )
    return text


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
    result, _, _ = await _call(
        "chat_with_tools",
        _tier_of(model, HEAVY),
        model,
        lambda b, m: b.chat_with_tools(
            messages,
            tools=tools,
            system=system,
            model=m,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            think=think,
        ),
    )
    # The next turn may run on a different backend; every call needs an
    # id so its tool result can be paired with it.
    for call in result.tool_calls:
        if not call.id:
            call.id = new_call_id()
    return result


async def chat_stream(
    messages: List[Dict[str, Any]],
    *,
    system: Optional[str] = None,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """Stream a heavy-tier reply. Falls back only before the first chunk."""
    errors: List[Tuple[str, str, LLMError]] = []
    for backend in _attempt_order():
        for model in _models_for(backend, HEAVY, None):
            started = False
            try:
                async for chunk in backend.chat_stream(
                    messages, system=system, temperature=temperature, model=model
                ):
                    started = True
                    yield chunk
            except LLMError as exc:
                if started:
                    raise
                errors.append((backend.NAME, model, exc))
                if isinstance(exc, LLMModelMissing):
                    continue
                if isinstance(exc, LLMUnavailable):
                    _mark_down(backend, exc)
                break
            else:
                _mark_up(backend)
                if errors:
                    logger.warning(
                        "[llm] chat_stream answered by fallback %s/%s", backend.NAME, model
                    )
                return
    raise _combined_error("chat_stream", errors)


__all__ = [
    "BACKENDS",
    "ChatWithToolsResult",
    "FAST",
    "HEAVY",
    "LLMError",
    "LLMModelMissing",
    "LLMTimeout",
    "LLMUnavailable",
    "ToolCall",
    "backend_states",
    "chain",
    "chat_oneshot",
    "chat_stream",
    "chat_with_tools",
    "fast_model",
    "generate",
    "generate_with_fallback",
    "ollama_warmup_enabled",
    "provider",
    "reset_health",
    "resolve_model",
]
