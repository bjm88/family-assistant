"""Fast contextual acknowledgement messages from the lightweight model.

Why this module exists
----------------------
The heavy agent model (``gemma4:26b``) takes anywhere from 3 s to 30 s
to converge — it has tools, RAG, schema, and a non-trivial reasoning
budget. On push-style surfaces like Telegram that's a long silence:
the user sends *"What time is Sara's piano lesson?"* and stares at a
quiet chat for 15 seconds before anything comes back.

This module uses the lightweight Gemma sibling (``gemma4:e2b``,
~10× faster on Apple Silicon) to mint a single short sentence we can
deliver inside ~1 s of the inbound — *"Looking up Sara's calendar..."*
The heavy model still does all the actual reasoning; the ack is purely
a latency hider so the user doesn't think the bot ignored them.

Usage pattern (race + ack)
--------------------------
Callers run the heavy agent in a worker thread and race it against
``AI_FAST_ACK_AFTER_SECONDS``. If the heavy agent finishes inside the
window the ack is skipped entirely (no point announcing work that's
already done). If it doesn't, the caller invokes
:func:`generate_contextual_ack_sync` to mint an ack and sends it as a
first reply, then the heavy result lands as a follow-up.

Why a SYNC entry point
----------------------
The Telegram + SMS inbox services already run inside
``asyncio.to_thread`` worker threads — they're not on an event loop
and don't want to be. A sync wrapper that owns its own loop keeps the
caller code straightforward (``ack = generate_contextual_ack_sync(...)``
slots in next to existing blocking helpers).

Failure mode: silent skip
-------------------------
Every error path returns ``None`` — Ollama not reachable, fast model
not pulled, timeout, empty completion, etc. The caller proceeds with
just the heavy reply. We deliberately do NOT fall back to the heavy
model for the ack: that would defeat the point of the entire flow
(we'd be running the slow model twice instead of once).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from ..config import get_settings
from .ollama import OllamaError, OllamaTimeout, OllamaUnavailable, _base, fast_model


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
#
# The system prompt is tightly constrained on purpose: e2b is small
# and easily wanders into actually trying to answer the question if we
# leave room for it to. We forbid greetings/sign-offs/questions back,
# and stuff the prompt with concrete examples so the output is
# stylistically consistent. Plain text only — Telegram replies go out
# without ``parse_mode`` and a stray ``*`` would print verbatim.

_SYSTEM_PROMPT = (
    "You are Avi, a household assistant. The user just sent a message, "
    "but the real answer will take a few more seconds to compute. "
    "Reply with ONE short sentence (under 90 characters) acknowledging "
    "what you are about to do, in present-continuous tense.\n"
    "\n"
    "HARD RULES:\n"
    "* DO NOT answer the question. Just say what you're working on.\n"
    "* DO NOT greet the user, sign off, or apologise.\n"
    "* DO NOT ask the user any question back.\n"
    "* DO NOT promise a specific timeframe (no 'right back', 'in a sec').\n"
    "* Plain text only — no Markdown, no emoji, no quotes.\n"
    "* Pick a verb that fits: Looking up, Pulling, Drafting, Checking, "
    "Working on, Putting together.\n"
    "* If a SUBJECT is obvious from the user's message (a person, an "
    "event, a topic), reference it. Never invent details that aren't "
    "in the message.\n"
    "\n"
    "Examples:\n"
    "  USER: What time is Sara's piano lesson tomorrow?\n"
    "  YOU:  Looking up Sara's calendar.\n"
    "\n"
    "  USER: Send Mike an email about practice tonight.\n"
    "  YOU:  Drafting that email to Mike.\n"
    "\n"
    "  USER: How is Daisy doing health-wise?\n"
    "  YOU:  Pulling up Daisy's health record.\n"
    "\n"
    "  USER: Plan a long weekend in Tahoe next month.\n"
    "  YOU:  Working on a Tahoe trip plan.\n"
    "\n"
    "  USER: hi\n"
    "  YOU:  Checking in.\n"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_user_prompt(
    *,
    surface: str,
    sender_display_name: Optional[str],
    last_user_message: str,
) -> str:
    """Format the per-call user message for the fast model."""
    sender_hint = sender_display_name or "the user"
    return (
        f"Surface: {surface}\n"
        f"Sender: {sender_hint}\n"
        f"Their message:\n"
        f"{last_user_message.strip()}\n"
        "\n"
        "Now reply with the one-sentence acknowledgement (no quotes)."
    )


def generate_contextual_ack_sync(
    *,
    surface: str,
    sender_display_name: Optional[str],
    last_user_message: str,
    timeout_seconds: Optional[float] = None,
) -> Optional[str]:
    """Return a 1-sentence ack from the fast model, or ``None`` on failure.

    Synchronous entry point — owns its own event loop so the inbox
    services (which run inside ``asyncio.to_thread`` worker threads
    and have no ambient loop) can call it as a normal blocking
    function. Bounded by :setting:`AI_FAST_ACK_TIMEOUT_SECONDS`
    because a slow ack call would defeat its own purpose.

    Returns ``None`` on:

    * the feature flag being off
    * the fast model not being pulled in Ollama (we deliberately do
      NOT fall back to the heavy model — that would mean running the
      26B twice for one inbound)
    * Ollama unreachable or HTTP error
    * the call exceeding ``timeout_seconds``
    * the model returning an empty / whitespace-only completion

    Parameters
    ----------
    surface
        ``"telegram"`` / ``"sms"`` / ``"chat"`` etc. Embedded in the
        prompt so the model can lightly tone-shift if it wants to
        (it usually doesn't, which is fine).
    sender_display_name
        Best name we have for the speaker — used in the prompt so the
        model can personalise (*"Looking up your calendar..."*) but
        never required.
    last_user_message
        The actual inbound text we're acknowledging.
    timeout_seconds
        Override the default from settings. Mostly useful in tests.

    See also
    --------
    :func:`generate_contextual_ack_async` — the async-native sibling
    used by callers that already live on an event loop (e.g. the live
    chat SSE endpoint). They share the prompt + timeout + cleanup
    logic so behaviour stays identical across surfaces.
    """
    settings = get_settings()
    if not settings.AI_FAST_ACK_ENABLED:
        return None
    if not last_user_message or not last_user_message.strip():
        return None

    cap = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.AI_FAST_ACK_TIMEOUT_SECONDS
    )
    user_prompt = _build_user_prompt(
        surface=surface,
        sender_display_name=sender_display_name,
        last_user_message=last_user_message,
    )

    async def _call() -> str:
        return await asyncio.wait_for(
            _chat_oneshot(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                model=fast_model(),
                temperature=0.4,
                max_tokens=80,
            ),
            timeout=cap,
        )

    try:
        text = _run_async(_call())
    except asyncio.TimeoutError:
        logger.debug(
            "[fast_ack] e2b call exceeded %.1fs timeout — skipping ack",
            cap,
        )
        return None
    except OllamaUnavailable as exc:
        # Fast model isn't pulled, or Ollama itself is down. Expected
        # in dev / fresh installs; never bubble up.
        logger.debug("[fast_ack] %s — skipping ack", exc)
        return None
    except OllamaError as exc:
        logger.warning("[fast_ack] Ollama error %s — skipping ack", exc)
        return None
    except Exception:  # noqa: BLE001 - last-resort safety
        logger.exception("[fast_ack] unexpected error — skipping ack")
        return None

    cleaned = _clean_ack_text(text)
    logger.info(
        "[fast_ack] sync done surface=%s model=%s ack_chars=%d",
        surface,
        fast_model(),
        len(cleaned or ""),
    )
    return cleaned


async def generate_contextual_ack_async(
    *,
    surface: str,
    sender_display_name: Optional[str],
    last_user_message: str,
    timeout_seconds: Optional[float] = None,
) -> Optional[str]:
    """Async-native counterpart of :func:`generate_contextual_ack_sync`.

    Use this from anywhere already on an event loop (e.g. the live
    chat SSE endpoint, where racing the heavy agent is best expressed
    with ``asyncio.create_task`` + ``asyncio.Event``). Shares the
    prompt, model selection, timeout cap, error handling, and output
    cleaning with the sync sibling so a given inbound produces the
    same ack regardless of which surface fired it.

    Returns ``None`` under exactly the same conditions as the sync
    version.
    """
    settings = get_settings()
    if not settings.AI_FAST_ACK_ENABLED:
        return None
    if not last_user_message or not last_user_message.strip():
        return None

    cap = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.AI_FAST_ACK_TIMEOUT_SECONDS
    )
    user_prompt = _build_user_prompt(
        surface=surface,
        sender_display_name=sender_display_name,
        last_user_message=last_user_message,
    )

    try:
        text = await asyncio.wait_for(
            _chat_oneshot(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                model=fast_model(),
                temperature=0.4,
                max_tokens=80,
            ),
            timeout=cap,
        )
    except asyncio.TimeoutError:
        logger.debug(
            "[fast_ack] e2b call exceeded %.1fs timeout — skipping ack",
            cap,
        )
        return None
    except OllamaUnavailable as exc:
        logger.debug("[fast_ack] %s — skipping ack", exc)
        return None
    except OllamaError as exc:
        logger.warning("[fast_ack] Ollama error %s — skipping ack", exc)
        return None
    except Exception:  # noqa: BLE001 - last-resort safety
        logger.exception("[fast_ack] unexpected error — skipping ack")
        return None

    cleaned = _clean_ack_text(text)
    logger.info(
        "[fast_ack] async done surface=%s model=%s ack_chars=%d",
        surface,
        fast_model(),
        len(cleaned or ""),
    )
    return cleaned


async def _chat_oneshot(
    *,
    system: str,
    user: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Single non-streaming Ollama ``/api/chat`` call returning ``content``.

    We deliberately bypass :func:`api.ai.ollama.generate` for two
    reasons:

    1. Gemma 4 is a *thinking* model. The raw ``/api/generate``
       endpoint does not surface the thinking-vs-reply split, so the
       model burns the entire ``num_predict`` budget on hidden
       reasoning and returns an empty ``response`` field. Using
       ``/api/chat`` (which applies the model's chat template and
       routes thinking into ``message.thinking``) makes the actual
       reply land in ``message.content``.
    2. We pass ``"think": false`` to skip the reasoning step entirely
       — for a 1-sentence ack the reasoning would take longer than
       the ack itself.

    Errors are mapped onto the same exception types the rest of
    :mod:`api.ai.ollama` raises so callers can keep their existing
    handlers.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        # Pin the fast model in Ollama's memory for an hour so the
        # next ack call doesn't pay the cold-load cost. Combined
        # with the lifespan warmup this means even the very first
        # ack of the day lands within ~500 ms instead of 3–4 s.
        "keep_alive": "1h",
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    # Same structured timeout shape as chat_with_tools: fail-fast on
    # connect, give the read the full per-call budget. The fast-ack
    # budget is intentionally short (the user is staring at a blank
    # live-chat bubble), so a stalled read should give up cleanly
    # instead of letting an httpcore.ReadTimeout bubble up raw.
    request_timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0)
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            r = await client.post(f"{_base()}/api/chat", json=payload)
    except httpx.ConnectError as exc:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise OllamaTimeout(
            f"Fast-ack model {model!r} did not respond within 10s "
            f"({type(exc).__name__})."
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


def _run_async(coro):
    """Run ``coro`` to completion using a fresh event loop.

    ``asyncio.run`` raises if there's already a running loop in the
    current thread. The inbox callers all run inside
    ``asyncio.to_thread`` worker threads with no ambient loop, so
    ``asyncio.run`` works for them — but using a fresh loop directly
    is also robust against future callers that DO have a loop nearby.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _clean_ack_text(raw: Optional[str]) -> Optional[str]:
    """Normalise the model output: strip quotes, cap length, reject empty."""
    text = (raw or "").strip()
    if not text:
        return None
    # The model often wraps replies in quotes despite the instruction.
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    if not text:
        return None
    # Hard cap. The prompt asks for under 90 chars; this is the safety
    # net for a model that runs away.
    if len(text) > 200:
        text = text[:197].rstrip() + "..."
    # Strip any stray Markdown the model emits despite "plain text only".
    text = text.replace("**", "").replace("__", "")
    return text


# ---------------------------------------------------------------------------
# Instant heuristic ack — no LLM, returns in microseconds
# ---------------------------------------------------------------------------


# Verb buckets keyed by leading-token / keyword. Order matters: the
# first matching bucket wins, so put the more specific actions
# (drafting, scheduling) before the generic lookup catch-all.
#
# The strings are deliberately neutral and surface-agnostic so this
# helper can stand in for the real e2b ack on any channel where we
# need *something* visible within milliseconds.
_HEURISTIC_BUCKETS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("send ", "email ", "draft ", "write ", "reply ", "respond ",
         "compose ", "message "),
        "Drafting that message.",
    ),
    (
        ("schedule ", "book ", "add ", "remind ", "create ", "set up ",
         "setup ", "plan "),
        "Setting that up.",
    ),
    (
        ("text ", "sms "),
        "Drafting that text.",
    ),
    (
        ("call ", "phone "),
        "Pulling that contact up.",
    ),
    (
        ("delete ", "remove ", "cancel ", "clear "),
        "Working on that update.",
    ),
    (
        ("update ", "change ", "edit ", "rename ", "fix "),
        "Working on that change.",
    ),
    (
        ("list ", "show ", "find ", "search ", "look ", "get ", "give me ",
         "fetch ", "pull ", "what ", "who ", "where ", "when ", "how ",
         "which ", "tell me", "do we have"),
        "Looking that up.",
    ),
)


def heuristic_ack(last_user_message: Optional[str]) -> str:
    """Pick a generic but vaguely-relevant ack string in microseconds.

    Used as the *instant* placeholder in front of the e2b call: even
    if Ollama is loaded down with the heavy 26b request and the
    contextual ack takes 3–4 s to come back, the user sees an
    immediately-relevant verb inside the assistant bubble within
    milliseconds. The contextual e2b ack replaces this when it
    lands; if the heavy reply wins the race the heuristic stays
    visible until the real content streams in.

    The match is case-insensitive on the leading 60 chars of the
    message so polite preambles ("hey, can you ...") still bucket
    correctly. Falls back to the safe generic when nothing matches.
    """
    head = (last_user_message or "").strip().lower()[:120]
    if not head:
        return "Working on it."
    for keywords, ack in _HEURISTIC_BUCKETS:
        for kw in keywords:
            if kw in head:
                return ack
    return "Working on it."


__all__ = [
    "generate_contextual_ack_sync",
    "generate_contextual_ack_async",
    "heuristic_ack",
]
