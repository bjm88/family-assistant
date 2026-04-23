"""Fast-path "skip the agent loop" web-search shortcut.

Why this module exists
----------------------
The heavy local Gemma agent loop is expensive — for an inbound like
*"What's the weather in Asheville this weekend?"* we burn:

1. ~3-5 s loading the full system prompt (persona + safety + RAG +
   schema + house context + capabilities) into the 26B model.
2. One tool-call round trip so the agent decides to call `web_search`.
3. ~2-3 s for our `web_search` tool to round-trip Gemini's
   ``google_search`` grounding.
4. ~2-4 s for the heavy model to re-read Gemini's summary and write
   a 2-sentence reply.

For *pure* web-lookup asks — questions that need NO household
context (no calendar, no contacts, no DB facts, no email drafting,
no task tracking) — steps 1, 2 and 4 are pure overhead. The user's
answer is whatever Gemini just synthesised; the heavy model adds
no value beyond restating it.

This module lets us short-circuit those turns:

* :func:`classify` runs the lightweight Gemma sibling
  (``gemma3:e2b``-class, ~300 ms warm) on the user message and
  returns ``True`` only when it's confident the ask is "pure web
  lookup, no household context required".
* :func:`run` then calls Gemini's grounded answer directly via
  :func:`api.integrations.web_search.grounded_chat_answer`, which
  reuses the same Gemini provider + retry-with-backoff layer the
  ``web_search`` agent tool already uses.
* :func:`try_shortcut` is the convenience that combines them and is
  what every caller should reach for.

Failure modes degrade gracefully
--------------------------------
EVERY failure (classifier timeout, classifier says "agent",
Gemini overload after retries, missing key, non-Gemini provider,
fast model not pulled, …) returns ``None`` from :func:`try_shortcut`
and the caller falls through to the existing heavy-agent loop. The
shortcut is purely an opportunistic latency win; it never
*replaces* the agent — it just front-runs it when it can.

Per the design choice "minimal audit": the shortcut deliberately
does NOT create an :class:`AgentTask` row. Surfaces that need to
log the assistant's reply (live chat -> ``LiveSessionMessage``,
SMS / Telegram / email -> their own audit tables) do so
themselves; this module only owns the classify-and-fetch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from ..config import get_settings
from ..integrations import web_search
from .ollama import OllamaError, OllamaUnavailable, _base, fast_model


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classifier prompt
# ---------------------------------------------------------------------------
#
# The fast model is instructed to emit one of two single tokens.
# Anything else (full-sentence rambles, JSON, etc.) is treated as
# `AGENT` so we err on the safe side. Examples are deliberately
# concrete so the model can pattern-match cleanly without burning
# its tiny attention budget on edge cases.

_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a router for a family-assistant chatbot. The chatbot "
    "has a heavy local model for household tasks (calendar, "
    "contacts, drafting emails, tasks, family facts) AND a separate "
    "fast Gemini web-search shortcut for pure look-it-up questions.\n"
    "\n"
    "Your job: read the user's latest message and decide which path "
    "should handle it. Reply with EXACTLY ONE WORD, no punctuation:\n"
    "\n"
    "  WEB    -- the message is a pure web-lookup / current-events / "
    "factual question whose answer is on the open internet and that "
    "does NOT mention a household member by name, a personal "
    "calendar event, a household task, a vehicle, a residence, an "
    "email to send, or any other private household context.\n"
    "\n"
    "  AGENT  -- the message references the household, asks the "
    "assistant to take an action (send email, schedule, add task, "
    "update record), is conversational/personal, references a "
    "person by name, mentions 'my' / 'our' / 'us' / a specific "
    "family member, or otherwise needs household context.\n"
    "\n"
    "Default to AGENT when uncertain. False positives (sending a "
    "household question to WEB) are MUCH worse than false negatives "
    "(sending a generic question to AGENT — slower but always "
    "works).\n"
    "\n"
    "Examples:\n"
    "\n"
    "  USER: What's the weather in Asheville this weekend?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Who won the Knicks game last night?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Look up the new tax brackets for 2026.\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Find me a recipe for sheet-pan salmon.\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: What's the closing price of Tesla today?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Latest news on the SpaceX Starship test.\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: What time does Trader Joe's close today?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Who is the current U.S. Speaker of the House?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Send Mike an email about practice tonight.\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: What's on my calendar tomorrow?\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: Add a task to renew the passport.\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: What's Sarah's email?\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: How is Daisy doing health-wise?\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: Hi\n"
    "  YOU:  AGENT\n"
    "\n"
    "  USER: Plan a long weekend in Tahoe next month.\n"
    "  YOU:  AGENT\n"
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


async def classify(message: str) -> bool:
    """Return ``True`` when the message looks like a pure web lookup.

    Bounded by ``AI_WEB_SEARCH_SHORTCUT_CLASSIFIER_TIMEOUT_S``; any
    timeout / Ollama error / unexpected output returns ``False`` so
    the caller falls through to the heavy agent (the safe default).
    """
    text = (message or "").strip()
    if not text:
        return False

    settings = get_settings()
    if not settings.AI_WEB_SEARCH_SHORTCUT_ENABLED:
        return False

    user_prompt = (
        "Classify this single message. Reply with exactly one word "
        "— WEB or AGENT — and nothing else.\n\n"
        f"USER: {text}\n"
        "YOU:"
    )

    try:
        raw = await asyncio.wait_for(
            _chat_oneshot(
                system=_CLASSIFIER_SYSTEM_PROMPT,
                user=user_prompt,
                model=fast_model(),
                temperature=0.0,
                max_tokens=4,
            ),
            timeout=settings.AI_WEB_SEARCH_SHORTCUT_CLASSIFIER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[shortcut] classifier exceeded %.1fs — defaulting to AGENT",
            settings.AI_WEB_SEARCH_SHORTCUT_CLASSIFIER_TIMEOUT_S,
        )
        return False
    except OllamaUnavailable as exc:
        # Fast model not pulled, Ollama down, etc. The heavy path can
        # still run via its own model; just skip the shortcut.
        logger.debug("[shortcut] %s — skipping shortcut", exc)
        return False
    except OllamaError as exc:
        logger.warning(
            "[shortcut] classifier Ollama error %s — falling through",
            exc,
        )
        return False
    except Exception:  # noqa: BLE001 - never crash the inbound
        logger.exception(
            "[shortcut] classifier crashed — falling through"
        )
        return False

    decision = (raw or "").strip().upper()
    # Some small models like to wrap the answer in quotes or add a
    # trailing period despite the instruction. Trim aggressively.
    for ch in ('"', "'", ".", ",", "!", "?", ":", ";"):
        decision = decision.strip(ch).strip()
    # Take the first whitespace-separated token so a chatty model
    # ("WEB - this is a search question") still routes correctly.
    decision = decision.split()[0] if decision else ""
    is_web = decision == "WEB"
    logger.info(
        "[shortcut] classify decision=%s (raw=%r) for %r",
        "WEB" if is_web else "AGENT",
        raw[:40] if raw else "",
        text[:80],
    )
    return is_web


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(message: str) -> Optional[str]:
    """Call Gemini grounded chat and return the answer, or ``None`` on failure.

    All errors (no provider, wrong provider, Gemini overload,
    transport hiccup) collapse to ``None`` so the caller falls
    through to the heavy agent. We log them so a persistent
    misconfiguration is still visible in the server logs.
    """
    text = (message or "").strip()
    if not text:
        return None
    logger.info(
        "[shortcut] run start path=gemini_grounded prompt_chars=%d",
        len(text),
    )
    started = time.monotonic()
    try:
        answer = await web_search.grounded_chat_answer(text)
        logger.info(
            "[shortcut] run done path=gemini_grounded duration_ms=%d "
            "answer_chars=%d",
            int((time.monotonic() - started) * 1000),
            len(answer or ""),
        )
        return answer
    except web_search.SearchUnavailable as exc:
        logger.info(
            "[shortcut] run unavailable duration_ms=%d reason=%s — "
            "falling through to heavy agent",
            int((time.monotonic() - started) * 1000),
            exc,
        )
        return None
    except Exception:  # noqa: BLE001 - last-ditch safety
        logger.exception(
            "[shortcut] run crashed in grounded_chat_answer — falling "
            "through to heavy agent"
        )
        return None


async def try_shortcut(message: str) -> Optional[str]:
    """Classify-then-run, with every failure returning ``None``.

    Returns the final answer text (already polished by the Gemini
    prompt for spoken-English / no-Markdown / 1-3 sentences) when
    the shortcut succeeds, ``None`` otherwise. Callers should treat
    ``None`` as "fall through to the existing heavy-agent path".

    The whole call is cheap on a "no" decision (one ~300 ms fast-
    Gemma round trip and a return), so it's safe to wire into every
    user-facing surface unconditionally — when the classifier votes
    AGENT, the only cost is the classifier itself.

    Wraps every failure in a single broad ``except`` so callers
    don't have to duplicate the same defensive ``try`` block. The
    inner ``classify`` and ``run`` helpers each catch their known
    failure modes (timeouts, Ollama unavailable, Gemini quota); this
    outer net is a belt-and-suspenders against anything new — the
    shortcut is a pure latency win and must never crash the surface
    that invoked it.
    """
    settings = get_settings()
    if not settings.AI_WEB_SEARCH_SHORTCUT_ENABLED:
        logger.debug(
            "[shortcut] disabled by AI_WEB_SEARCH_SHORTCUT_ENABLED=false"
        )
        return None

    try:
        is_web = await classify(message)
        if not is_web:
            return None
        return await run(message)
    except Exception:  # noqa: BLE001 - shortcut must never break the caller
        logger.exception(
            "[shortcut] try_shortcut crashed — falling through"
        )
        return None


def try_shortcut_sync(message: str) -> Optional[str]:
    """Synchronous wrapper for the SMS / Telegram / email inbox flows.

    Those three surfaces run inside ``asyncio.to_thread`` worker
    threads with no ambient event loop, so they can't ``await`` the
    async API. We follow the same own-its-own-loop pattern
    :func:`fast_ack.generate_contextual_ack_sync` uses.

    Every error returns ``None`` so the caller falls through to the
    existing heavy agent — the shortcut is a pure latency win, never
    a behavioural prerequisite.
    """
    if not (message or "").strip():
        return None
    settings = get_settings()
    if not settings.AI_WEB_SEARCH_SHORTCUT_ENABLED:
        logger.debug(
            "[shortcut] disabled by AI_WEB_SEARCH_SHORTCUT_ENABLED=false"
        )
        return None

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(try_shortcut(message))
    except Exception:  # noqa: BLE001 - shortcut never breaks the inbound
        logger.exception(
            "[shortcut] try_shortcut_sync crashed — falling through"
        )
        return None
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Internal: thin Ollama /api/chat wrapper for the classifier
# ---------------------------------------------------------------------------
#
# We deliberately bypass `api.ai.ollama.generate` for the same
# reasons fast_ack does: the gemma chat template routes "thinking"
# tokens into a separate channel, so a one-word reply needs the
# /api/chat endpoint with `think=False` to actually appear in
# `message.content`.


async def _chat_oneshot(
    *,
    system: str,
    user: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        # Same hour-long pin the fast-ack path uses so the next
        # classification doesn't pay a cold-load cost.
        "keep_alive": "1h",
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{_base()}/api/chat", json=payload)
    except httpx.ConnectError as exc:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {exc}"
        ) from exc

    if r.status_code == 404:
        raise OllamaUnavailable(
            f"Model '{model}' is not pulled. Run `ollama pull {model}`."
        )
    if r.status_code >= 400:
        raise OllamaError(f"Ollama returned {r.status_code}: {r.text[:400]}")

    data = r.json()
    message = data.get("message") or {}
    return (message.get("content") or "").strip()


__all__ = ["classify", "run", "try_shortcut", "try_shortcut_sync"]
