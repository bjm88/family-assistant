"""Fast-path "skip the agent loop" router for simple family Q&A.

Why this module exists
----------------------
The heavyweight agent loop is the correct answer for tool-using asks
(send an email, schedule, kick off a research task), attachments, and
multi-step reasoning — but it's overkill for the common case of
*"answer a question about my family using what you already know"*:

* Looking up a family member's goal.
* Reminding the user who their doctor is.
* Listing vehicles / insurance policies / pets.
* Reading back a column's value the RAG block already contains.

Those turns don't need tool calls, don't need a 26B model, and don't
need a three-round plan/execute/observe loop. A fine-tuned
lightweight Gemma (see ``scripts/ai_training/``) trained on our
schema + API surface + Avi's voice can answer them directly in a
fraction of the time.

This module implements the same pattern as
:mod:`api.ai.web_search_shortcut`:

* :func:`classify` asks the lightweight model (via ``fast_model()``)
  whether a message is family-Q&A or needs the heavy agent.
* :func:`run` calls the *fine-tuned* model
  (:setting:`AI_FAMILY_QA_MODEL`) with the RAG block + persona system
  prompt and returns the answer text.
* :func:`try_shortcut` composes them. Returns ``None`` on every
  failure so the caller transparently falls through to the heavy
  agent. The shortcut is a pure latency win — never a correctness
  prerequisite.

Design notes
------------
* The shortcut does NOT create an :class:`AgentTask` row (same
  minimal-audit choice as the web-search shortcut). The caller is
  responsible for logging the user + assistant messages to the
  ``LiveSession`` transcript.
* The shortcut runs **after** the web-search shortcut in the chat
  endpoint, so pure web-lookup questions still take the Gemini
  grounded path. The family-QA classifier is only consulted when
  the web-search classifier said "AGENT".
* The fine-tuned model received negative examples during training
  that teach it to emit a short "escalating to the full agent"
  reply when it sees an ask outside its scope. Those are treated
  as a fall-through: :func:`run` detects the escalation phrase and
  returns ``None`` so the caller invokes the heavy agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from ..config import get_settings
from . import authz, chat_prompts
from . import prompts as prompts_mod
from .ollama import (
    OllamaError,
    OllamaUnavailable,
    _base,
    fast_model,
    system_prompt_for_avi,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classifier prompt — mirrors web_search_shortcut._CLASSIFIER_SYSTEM_PROMPT
# in style and rigor. Two single-token answers; default to HEAVY.
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a router for a family-assistant chatbot. The chatbot has:\n"
    "  - a FAST local model that knows the family's people, goals, "
    "pets, residences, vehicles, insurance, and the database schema "
    "— good at answering factual questions from household context, "
    "reading back stored data (INCLUDING sensitive values like SSN "
    "and account numbers when the speaker is authorised; the "
    "backend already redacts what they shouldn't see), and chatting "
    "casually.\n"
    "  - a HEAVY agent with tools (Gmail, Calendar, web search, task "
    "creation, attachment analysis, monitoring jobs).\n"
    "\n"
    "Read the user's latest message and decide which path handles it. "
    "Reply with EXACTLY ONE WORD, nothing else:\n"
    "\n"
    "  FAMILY -- a factual/recall question about the household "
    "(including \"what's my SSN?\", \"what's my child's social "
    "security number?\", \"remind me of our policy number\" — the "
    "RAG block already contains the values the speaker is allowed "
    "to see), a casual acknowledgement (hi / thanks / ok), a "
    "schema/meta question about how the app stores data, or small "
    "talk. Nothing that needs a tool.\n"
    "\n"
    "  HEAVY  -- the message asks the assistant to TAKE AN ACTION "
    "(send email, schedule, add/update/close a task, open the gate, "
    "invite someone), analyse an attachment, do web research, run a "
    "monitoring job, or do multi-step planning.\n"
    "\n"
    "Default to HEAVY when uncertain. A false positive (routing a "
    "tool-needing ask to FAMILY) means the user gets a wrong answer; "
    "a false negative (routing a simple recall question to HEAVY) "
    "just means the user waits a few seconds longer — always "
    "preferable.\n"
    "\n"
    "Examples:\n"
    "\n"
    "  USER: who lives here?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: what's sara's top goal?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: when does the subaru's registration expire?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: which doctor does theo see?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: remind me of our homeowners policy number\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: what's my social security number?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: what's jax's social security number?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: what does the goals table store?\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: thanks\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: hey\n"
    "  YOU:  FAMILY\n"
    "\n"
    "  USER: send sara a note about dinner\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: what's on my calendar tomorrow?\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: add a task to renew the passport\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: analyse this PDF\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: research summer camps for theo\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: open the gate\n"
    "  YOU:  HEAVY\n"
)


# Phrases the fine-tuned model was trained to emit when it wants to
# escalate. When we see these at the start of its reply we treat the
# shortcut as a fall-through rather than delivering the half-answer.
_ESCALATION_TOKENS = (
    "handing this to the full agent",
    "handing this to the agent",
    "handing off to the agent",
    "passing to the agent",
    "passing to the full agent",
    "routing to the full agent",
    "i'll hand this off",
    "i'll hand it off",
    "i'll hand off",
    "needs the email tool",
    "needs the full agent",
    "go through the agent",
    "goes through the agent",
    "belongs to the full agent",
    "outside my scope",
    "i don't track",
    "i can't see images",
)


def _looks_like_escalation(text: str) -> bool:
    low = (text or "").strip().lower()
    # Guard against over-matching — the fine-tune answer must be SHORT
    # (the escalation template is always 1-2 sentences). Longer replies
    # are almost certainly real answers that happened to contain a
    # near-match phrase.
    if len(low) > 300:
        return False
    return any(tok in low for tok in _ESCALATION_TOKENS)


# ---------------------------------------------------------------------------
# Shared Ollama helpers (kept local so we don't leak module-private
# routines into the shared client).
# ---------------------------------------------------------------------------


async def _chat_oneshot(
    *,
    system: str,
    user: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
) -> str:
    """Minimal non-streaming ``/api/chat`` call against ``model``.

    Mirrors :func:`api.ai.web_search_shortcut._chat_oneshot` so both
    shortcuts share the exact same Ollama call shape (``think:false``,
    ``keep_alive="1h"``) and bypass :mod:`api.ai.ollama`'s defaulting
    to the heavy model tag.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "keep_alive": "1h",
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 2.0) as client:
            r = await client.post(f"{_base()}/api/chat", json=payload)
    except httpx.ConnectError as exc:
        raise OllamaUnavailable(
            f"Ollama at {_base()} is not responding: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise OllamaError(
            f"Ollama call to {model!r} exceeded {timeout_s:.1f}s: {exc}"
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


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


async def classify(message: str) -> bool:
    """Return ``True`` when the message looks like simple family Q&A.

    Failure modes (timeout, Ollama down, fast model not pulled,
    unparseable output, anything unexpected) all return ``False`` so
    the caller routes to the heavy agent — which is always correct,
    just slower.
    """
    text = (message or "").strip()
    if not text:
        return False

    settings = get_settings()
    if not settings.AI_FAMILY_QA_SHORTCUT_ENABLED:
        return False
    if not (settings.AI_FAMILY_QA_MODEL or "").strip():
        # Feature flag is on but no model is registered — treat as off.
        return False

    user_prompt = (
        "Classify this single message. Reply with exactly one word — "
        "FAMILY or HEAVY — and nothing else.\n\n"
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
                timeout_s=settings.AI_FAMILY_QA_CLASSIFIER_TIMEOUT_S,
            ),
            timeout=settings.AI_FAMILY_QA_CLASSIFIER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[family_qa] classifier exceeded %.1fs — defaulting to HEAVY",
            settings.AI_FAMILY_QA_CLASSIFIER_TIMEOUT_S,
        )
        return False
    except OllamaUnavailable as exc:
        logger.debug("[family_qa] %s — skipping shortcut", exc)
        return False
    except OllamaError as exc:
        logger.warning(
            "[family_qa] classifier Ollama error %s — falling through", exc
        )
        return False
    except Exception:  # noqa: BLE001 - never crash the inbound
        logger.exception(
            "[family_qa] classifier crashed — falling through"
        )
        return False

    decision = (raw or "").strip().upper()
    for ch in ('"', "'", ".", ",", "!", "?", ":", ";"):
        decision = decision.strip(ch).strip()
    decision = decision.split()[0] if decision else ""
    is_family = decision == "FAMILY"
    logger.info(
        "[family_qa] classify decision=%s (raw=%r) for %r",
        "FAMILY" if is_family else "HEAVY",
        raw[:40] if raw else "",
        text[:80],
    )
    return is_family


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _build_system_prompt(
    db: Session,
    *,
    family_id: int,
    recognized_person_id: Optional[int],
    assistant_name: str,
    family_name: Optional[str],
    requestor_is_admin: bool = False,
) -> str:
    """System prompt for the fast-tier model.

    Intentionally **lighter** than the heavy-agent prompt:

    * Persona (same as heavy) so the voice matches across surfaces.
    * Safety envelope (same as heavy) so sensitive-data rules apply.
    * Speaker scope block — tells the fast model who is talking and
      what they're authorised to see. Without this the fast model
      would receive the (possibly decrypted) sensitive identifiers
      in RAG but have no understanding of why it's allowed to share
      them.
    * RAG block for live household state, with the same authz-gated
      decryption the heavy agent gets.
    * No capabilities block, no tool registry — the fast tier has no
      tools. An escalation rule tells it what to do when a tool is
      needed.
    """
    rag_block = chat_prompts.build_rag_block(
        db,
        family_id,
        recognized_person_id,
        requestor_is_admin=requestor_is_admin,
    )
    persona = system_prompt_for_avi(assistant_name, family_name)
    speaker_scope_block = authz.render_speaker_scope_block(
        authz.build_speaker_scope(
            db,
            speaker_person_id=recognized_person_id,
            family_id=family_id,
            requestor_is_admin=requestor_is_admin,
        )
    )

    escalation_rule = (
        "\n\n--- When NOT to answer ---\n"
        "You are the fast-path model for routine family Q&A. If the "
        "user asks you to take an action (send email, schedule, add "
        "a task, open the gate, invite someone), analyse an "
        "attachment, do web research, or anything that needs a tool "
        "— DO NOT attempt it. Reply with one short sentence saying "
        "you'll hand it off to the full agent (for example: "
        "\"Handing this to the full agent.\").\n"
        "\n"
        "Sensitive identifiers (SSN, full VIN, license plate, "
        "passport / driver's license / state ID number, bank account "
        "or routing number, insurance policy number) follow a "
        "DIFFERENT rule:\n"
        "  - If the requested value IS in the household-context "
        "block above (look for a 'Sensitive identifiers' section or "
        "an inline value), the privacy gate has ALREADY authorised "
        "this speaker — share it verbatim.\n"
        "  - If the value is NOT in the block, escalate with one "
        "short sentence ('Handing this to the full agent.'). The "
        "heavy agent has decrypt tools you don't and will run the "
        "household privacy gate itself — it will return the "
        "plaintext for an authorised speaker and a polite refusal "
        "otherwise. Do NOT refuse on the speaker's behalf and do "
        "NOT lecture about privacy: the missing-value case is "
        "almost always 'this field isn't denormalised into the "
        "prompt' (true for VIN / account / policy / passport / DL "
        "numbers — only their last-four shows here), not 'speaker "
        "isn't authorised'.\n"
        "\n"
        "Never invent answers or make up results."
    )

    parts = [
        prompts_mod.with_safety(persona),
        speaker_scope_block,
        "--- Household facts ---\n" + rag_block,
    ]
    return "\n\n".join(parts) + escalation_rule


async def run(
    message: str,
    *,
    db: Session,
    family_id: int,
    recognized_person_id: Optional[int],
    assistant_name: str,
    family_name: Optional[str],
    requestor_is_admin: bool = False,
) -> Optional[str]:
    """Call the fast-tier model and return the answer, or ``None``.

    Returns ``None`` when:

    * the fine-tuned model isn't pulled (``OllamaUnavailable``),
    * the call times out (covered by ``AI_FAMILY_QA_ANSWER_TIMEOUT_S``),
    * the model's own training told it to escalate (we detect the
      escalation phrase and fall through to heavy),
    * any unexpected crash happens.
    """
    text = (message or "").strip()
    if not text:
        return None
    settings = get_settings()
    model_tag = (settings.AI_FAMILY_QA_MODEL or "").strip()
    if not model_tag:
        return None

    logger.info(
        "[family_qa] run start model=%s family_id=%s person_id=%s "
        "prompt_chars=%d",
        model_tag,
        family_id,
        recognized_person_id,
        len(text),
    )
    started = time.monotonic()

    try:
        system = _build_system_prompt(
            db,
            family_id=family_id,
            recognized_person_id=recognized_person_id,
            assistant_name=assistant_name,
            family_name=family_name,
            requestor_is_admin=requestor_is_admin,
        )
    except Exception:  # noqa: BLE001 - RAG builder must never break the shortcut
        logger.exception(
            "[family_qa] system prompt build failed — falling through"
        )
        return None

    try:
        answer = await asyncio.wait_for(
            _chat_oneshot(
                system=system,
                user=text,
                model=model_tag,
                temperature=0.3,
                max_tokens=400,
                timeout_s=settings.AI_FAMILY_QA_ANSWER_TIMEOUT_S,
            ),
            timeout=settings.AI_FAMILY_QA_ANSWER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[family_qa] answer exceeded %.1fs — falling through to heavy",
            settings.AI_FAMILY_QA_ANSWER_TIMEOUT_S,
        )
        return None
    except OllamaUnavailable as exc:
        logger.info(
            "[family_qa] model %r unavailable: %s — falling through",
            model_tag,
            exc,
        )
        return None
    except OllamaError as exc:
        logger.warning(
            "[family_qa] Ollama error %s — falling through", exc
        )
        return None
    except Exception:  # noqa: BLE001 - last-ditch safety
        logger.exception(
            "[family_qa] run crashed in _chat_oneshot — falling through"
        )
        return None

    duration_ms = int((time.monotonic() - started) * 1000)

    if not answer:
        logger.info(
            "[family_qa] run empty duration_ms=%d — falling through",
            duration_ms,
        )
        return None

    if _looks_like_escalation(answer):
        logger.info(
            "[family_qa] model self-escalated duration_ms=%d reply=%r — "
            "falling through to heavy",
            duration_ms,
            answer[:120],
        )
        return None

    logger.info(
        "[family_qa] run done model=%s duration_ms=%d answer_chars=%d",
        model_tag,
        duration_ms,
        len(answer),
    )
    return answer


async def try_shortcut(
    message: str,
    *,
    db: Session,
    family_id: int,
    recognized_person_id: Optional[int],
    assistant_name: str,
    family_name: Optional[str],
    requestor_is_admin: bool = False,
) -> Optional[str]:
    """Classify-then-run, with every failure returning ``None``.

    Safe to call unconditionally from every surface. When the
    classifier votes HEAVY, the only cost is the ~200-500 ms
    classifier itself — no heavy-model tokens, no RAG build, no
    schema dump.
    """
    settings = get_settings()
    if not settings.AI_FAMILY_QA_SHORTCUT_ENABLED:
        logger.debug(
            "[family_qa] disabled by AI_FAMILY_QA_SHORTCUT_ENABLED=false"
        )
        return None
    if not (settings.AI_FAMILY_QA_MODEL or "").strip():
        logger.debug(
            "[family_qa] AI_FAMILY_QA_MODEL not set — shortcut inactive"
        )
        return None

    try:
        is_family = await classify(message)
        if not is_family:
            return None
        return await run(
            message,
            db=db,
            family_id=family_id,
            recognized_person_id=recognized_person_id,
            assistant_name=assistant_name,
            family_name=family_name,
            requestor_is_admin=requestor_is_admin,
        )
    except Exception:  # noqa: BLE001 - shortcut must never break the caller
        logger.exception(
            "[family_qa] try_shortcut crashed — falling through"
        )
        return None


__all__ = ["classify", "run", "try_shortcut"]
