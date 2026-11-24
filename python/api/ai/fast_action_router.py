"""Unified triage + fast-path action router.

Why this module exists
----------------------
Every inbound user message (live chat, SMS, WhatsApp, Telegram, email)
needs to be routed to ONE of four places:

* :mod:`api.ai.web_search_shortcut` — pure web-lookup asks
* :mod:`api.ai.family_qa_router`   — recall / chit-chat asks
* a per-intent action micro-loop   — calendar / task actions
* the heavy agent (:func:`api.ai.agent.run_agent`)

Historically each surface called the three shortcut modules in
sequence, each one running its own fast-model classifier round trip
before deciding to pass. That added 2-4 s of silent latency to *every*
turn before the user saw any reply, which broke the "Avi acknowledges
instantly" UX contract.

This module collapses the three classifiers into ONE combined router
call (single fast-model round trip) that returns ONE label:

* ``WEB``           — delegate to web_search_shortcut.run()
* ``FAMILY_QA``     — delegate to family_qa_router.run()
* ``CALENDAR_ADD``  — action micro-loop, calendar writer
* ``CALENDAR_LIST`` — action micro-loop, calendar readers
* ``TASK_ADD``      — action micro-loop, task writer
* ``TASK_LIST``     — action micro-loop, task readers
* ``TASK_UPDATE``   — action micro-loop, task patcher
* ``HEAVY``         — fall through to the heavy agent

:func:`try_triage` is what every surface should call; it returns a
:class:`TriageResult` with the reply text (when a shortcut handled
the turn) or ``label="HEAVY"`` (when the caller should run the heavy
agent). Every failure mode (classifier timeout, Ollama down, missing
fast model, runtime crash) collapses to ``HEAVY`` so the triage is
always safe to call — it's a pure latency win, never a correctness
prerequisite.

For action intents we drive the regular :func:`api.ai.agent.run_agent`
loop with three twists that turn it from a slow general-purpose loop
into a fast specialised one:

1. ``model_override`` points at the fast model
   (``AI_FAST_ACTION_MODEL`` if set, else ``fast_model()``).
2. The :class:`ToolRegistry` handed in contains *only* the 1-3 tools
   the intent actually needs (vs. the heavy agent's 21).
3. ``max_steps`` is clamped per-intent (read-only intents allow fewer
   steps than mutating ones).

The action path still **owns its own AgentTask row** so the
kanban / audit-step / SSE-replay plumbing the heavy agent provides
is genuinely there. Web / family-QA paths do NOT create an
AgentTask — they're zero-tool replies and the surface caller already
logs the assistant message to its own audit trail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from .. import models
from ..config import get_settings
from ..db import SessionLocal
from . import agent as agent_loop
from . import agent_drain, authz, family_qa_router, prompts as prompts_mod
from . import tools as agent_tools
from . import web_search_shortcut
from . import llm
from .llm import (
    LLMError,
    LLMUnavailable,
    fast_model,
)
from .persona import system_prompt_for_avi


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


LABEL_WEB = "WEB"
LABEL_FAMILY_QA = "FAMILY_QA"
LABEL_CALENDAR_ADD = "CALENDAR_ADD"
LABEL_CALENDAR_LIST = "CALENDAR_LIST"
LABEL_TASK_ADD = "TASK_ADD"
LABEL_TASK_LIST = "TASK_LIST"
LABEL_TASK_UPDATE = "TASK_UPDATE"
LABEL_EMAIL_SEND = "EMAIL_SEND"
LABEL_HEAVY = "HEAVY"


ACTION_LABELS: Tuple[str, ...] = (
    LABEL_CALENDAR_ADD,
    LABEL_CALENDAR_LIST,
    LABEL_TASK_ADD,
    LABEL_TASK_LIST,
    LABEL_TASK_UPDATE,
    LABEL_EMAIL_SEND,
)


ALL_LABELS: Tuple[str, ...] = (
    LABEL_WEB,
    LABEL_FAMILY_QA,
    *ACTION_LABELS,
    LABEL_HEAVY,
)


# Read-only intents do less work; cap their loop tighter so a confused
# fast model can't burn the budget chaining redundant list calls.
_READ_ONLY_INTENTS: frozenset[str] = frozenset(
    {LABEL_CALENDAR_LIST, LABEL_TASK_LIST}
)


# Per-intent step caps. Defaults sized for the typical "lookup then
# act" pattern; read-only intents get a tighter cap because they
# should never need more than "list + reply".
_INTENT_MAX_STEPS: dict[str, int] = {
    LABEL_CALENDAR_ADD: 3,
    LABEL_CALENDAR_LIST: 2,
    LABEL_TASK_ADD: 3,
    LABEL_TASK_LIST: 2,
    LABEL_TASK_UPDATE: 4,
    LABEL_EMAIL_SEND: 3,
}


# Per-intent tool subsets. The action runner builds a sub-registry
# containing ONLY these tools so the fast model isn't tempted to
# reach for anything outside its lane.
_INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    LABEL_CALENDAR_ADD: ("lookup_person", "calendar_create_event"),
    LABEL_CALENDAR_LIST: (
        "lookup_person",
        "calendar_list_upcoming",
        "calendar_check_availability",
        "calendar_list_for_person",
        "calendar_find_free_slots",
    ),
    LABEL_TASK_ADD: ("lookup_person", "task_create", "task_add_follower"),
    LABEL_TASK_LIST: ("lookup_person", "task_list", "task_get"),
    LABEL_TASK_UPDATE: (
        "lookup_person",
        "task_list",
        "task_get",
        "task_update",
        "task_add_comment",
    ),
    LABEL_EMAIL_SEND: ("lookup_person", "gmail_send"),
}


_INTENT_HINTS: dict[str, str] = {
    LABEL_CALENDAR_ADD: (
        "The user wants to ADD a calendar event. Use "
        "`calendar_create_event` to write the event to the SPEAKER's "
        "calendar. If the event involves another household member "
        "(e.g. 'with Sarah', 'for dmw') call `lookup_person` first "
        "only when you need their email for an invitee — otherwise "
        "skip it and just create the event on the speaker's calendar. "
        "ISO 8601 with the family timezone offset is required for "
        "start_time / end_time; default duration is 60 minutes when "
        "the user doesn't say."
    ),
    LABEL_CALENDAR_LIST: (
        "The user wants to READ the calendar. Use "
        "`calendar_list_upcoming` for 'what's coming up' or "
        "`calendar_list_for_person` when they ask about a specific "
        "person — call `lookup_person` first to resolve the name. "
        "Use `calendar_check_availability` for 'is X free at …' and "
        "`calendar_find_free_slots` for 'when can X do a …'."
    ),
    LABEL_TASK_ADD: (
        "The user wants to ADD a task. Call `task_create` with a "
        "concise title and any priority / due-date the user mentioned. "
        "Defaults: priority='normal', the speaker is the creator AND "
        "assignee unless told otherwise. If the user explicitly names "
        "an assignee or follower call `lookup_person` first to "
        "resolve the person_id."
    ),
    LABEL_TASK_LIST: (
        "The user wants to LIST tasks. Use `task_list` with the "
        "right filters: ``mine_only=true`` for 'my tasks' / 'what's on "
        "my plate', ``priority='urgent'`` for 'what's urgent?'. Call "
        "`task_get` only when the user asked for detail on ONE task."
    ),
    LABEL_TASK_UPDATE: (
        "The user wants to UPDATE a task. Use `task_list` (with `q=` "
        "for free-text matching the user's description, e.g. "
        "q='gate' for 'the gate task') to find the right task_id, "
        "then `task_update` with only the fields that changed. "
        "Setting status='done' automatically stamps completed_at."
    ),
    LABEL_EMAIL_SEND: (
        "The user wants to SEND an email. Call `lookup_person` first "
        "to resolve the recipient's real address when they named a "
        "household member. Then call `gmail_send` with a fully-drafted "
        "subject and body. Do not invent an email address. Confirm "
        "in one short sentence after the send succeeds."
    ),
}


# ---------------------------------------------------------------------------
# Combined classifier
# ---------------------------------------------------------------------------


_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a router for a family-assistant chatbot. Decide which "
    "path handles the user's message. Reply with EXACTLY ONE LABEL "
    "from this list, nothing else:\n"
    "\n"
    "  WEB           -- pure web lookup with NO household context "
    "(weather, sports, recipes, news, current events, prices, public "
    "facts that don't mention a family member by name).\n"
    "\n"
    "  FAMILY_QA     -- factual question about the household using "
    "stored data (who lives here, what's Sarah's email, when does "
    "the Subaru registration expire, what's my SSN), a quick "
    "schema / meta question, or casual chit-chat (hi, thanks, ok).\n"
    "\n"
    "  CALENDAR_ADD  -- add / schedule / book / put on the calendar "
    "an event, hold, reminder, appointment, focus block.\n"
    "\n"
    "  CALENDAR_LIST -- read the calendar: 'what's on …', 'when is "
    "…', 'is X free …', 'find a time for …'.\n"
    "\n"
    "  TASK_ADD      -- add / create a kanban task, todo, or "
    "reminder ('remind me to', 'add a task to', 'we should').\n"
    "\n"
    "  TASK_LIST     -- list / show / what are my tasks / what's "
    "on my plate / what's urgent.\n"
    "\n"
    "  TASK_UPDATE   -- change a task's status, priority, owner, "
    "title, or notes ('mark X done', 'bump X to urgent', "
    "'reassign X to Sarah').\n"
    "\n"
    "  EMAIL_SEND    -- send / draft / write an email to a named "
    "person ('send Mom a note', 'email Ben about practice').\n"
    "\n"
    "  HEAVY         -- anything else: attachments, multi-step "
    "planning, recurring monitoring jobs, adding / changing / "
    "finishing a goal, medication, ID document, saved document, or "
    "someone's photo / profile picture (those are NOT tasks), "
    "anything that needs a tool not listed "
    "above, anything ambiguous.\n"
    "\n"
    "Default to HEAVY when uncertain. A false positive (running a "
    "shortcut on a turn that needs the heavy agent) means the user "
    "gets a wrong / incomplete answer; a false negative just means "
    "the user waits a few seconds longer.\n"
    "\n"
    "Examples:\n"
    "\n"
    "  USER: What's the weather in Asheville this weekend?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Who won the Knicks game last night?\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Find me a recipe for sheet-pan salmon.\n"
    "  YOU:  WEB\n"
    "\n"
    "  USER: Who lives here?\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: What's Sarah's email?\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: When does the Subaru registration expire?\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: what's my social security number?\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: thanks\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: hi\n"
    "  YOU:  FAMILY_QA\n"
    "\n"
    "  USER: Add a hold on my calendar next Tuesday at 2pm.\n"
    "  YOU:  CALENDAR_ADD\n"
    "\n"
    "  USER: Block 90 minutes of focus time tomorrow morning at 9.\n"
    "  YOU:  CALENDAR_ADD\n"
    "\n"
    "  USER: What's on my calendar tomorrow?\n"
    "  YOU:  CALENDAR_LIST\n"
    "\n"
    "  USER: Is Sarah free Friday afternoon?\n"
    "  YOU:  CALENDAR_LIST\n"
    "\n"
    "  USER: Add a task to renew the passport.\n"
    "  YOU:  TASK_ADD\n"
    "\n"
    "  USER: Remind me to call the plumber.\n"
    "  YOU:  TASK_ADD\n"
    "\n"
    "  USER: What are my tasks?\n"
    "  YOU:  TASK_LIST\n"
    "\n"
    "  USER: What's urgent for me right now?\n"
    "  YOU:  TASK_LIST\n"
    "\n"
    "  USER: Mark the gate task as done.\n"
    "  YOU:  TASK_UPDATE\n"
    "\n"
    "  USER: Bump the passport task to urgent.\n"
    "  YOU:  TASK_UPDATE\n"
    "\n"
    "  USER: Send Mom a note thanking her for dinner.\n"
    "  YOU:  EMAIL_SEND\n"
    "\n"
    "  USER: Email Ben a one-line summary of tomorrow.\n"
    "  YOU:  EMAIL_SEND\n"
    "\n"
    "  USER: Monitor for good Yankees ticket deals in May.\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: Make a task to review this house, details attached as a PDF.\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: Plan a long weekend in Tahoe next month.\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: Jackson started Zyrtec 10mg daily.\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: I finished my half-marathon goal.\n"
    "  YOU:  HEAVY\n"
    "\n"
    "  USER: Add Layla's United MileagePlus number AB123456.\n"
    "  YOU:  HEAVY\n"
)


# Tolerant label aliases — small fast models occasionally return
# near-misses we can safely re-map without losing the intent.
_LABEL_ALIASES: dict[str, str] = {
    # action variants
    "ADD_CALENDAR": LABEL_CALENDAR_ADD,
    "CALENDAR": LABEL_CALENDAR_LIST,
    "TASK": LABEL_TASK_LIST,
    "TASKS": LABEL_TASK_LIST,
    "LIST_TASKS": LABEL_TASK_LIST,
    "ADD_TASK": LABEL_TASK_ADD,
    "CREATE_TASK": LABEL_TASK_ADD,
    "UPDATE_TASK": LABEL_TASK_UPDATE,
    "SEND_EMAIL": LABEL_EMAIL_SEND,
    "EMAIL": LABEL_EMAIL_SEND,
    "GMAIL": LABEL_EMAIL_SEND,
    # legacy NONE -> HEAVY mapping for backward compat with old prompt
    "NONE": LABEL_HEAVY,
    "AGENT": LABEL_HEAVY,
    # web variants
    "WEB_SEARCH": LABEL_WEB,
    "SEARCH": LABEL_WEB,
    # family variants
    "FAMILY": LABEL_FAMILY_QA,
    "RECALL": LABEL_FAMILY_QA,
    "QA": LABEL_FAMILY_QA,
}


async def classify_triage(message: str) -> str:
    """Run the combined classifier; return one of :data:`ALL_LABELS`.

    All failure modes (timeout, Ollama down, unparseable reply,
    feature flag off) return :data:`LABEL_HEAVY` so callers can treat
    HEAVY uniformly as "no shortcut available".
    """
    text = (message or "").strip()
    if not text:
        return LABEL_HEAVY
    settings = get_settings()
    if not settings.AI_FAST_ACTION_SHORTCUT_ENABLED:
        return LABEL_HEAVY

    model_tag = llm.resolve_model(settings.AI_FAST_ACTION_MODEL, fast=True)

    user_prompt = (
        "Classify this single message. Reply with EXACTLY ONE WORD "
        "from the allowed list (WEB, FAMILY_QA, CALENDAR_ADD, "
        "CALENDAR_LIST, TASK_ADD, TASK_LIST, TASK_UPDATE, "
        "EMAIL_SEND, HEAVY) "
        "and nothing else.\n\n"
        f"USER: {text}\n"
        "YOU:"
    )

    try:
        raw = await asyncio.wait_for(
            llm.chat_oneshot(
                system=_CLASSIFIER_SYSTEM_PROMPT,
                user=user_prompt,
                model=model_tag,
                temperature=0.0,
                max_tokens=6,
                timeout_s=settings.AI_FAST_ACTION_CLASSIFIER_TIMEOUT_S,
            ),
            timeout=settings.AI_FAST_ACTION_CLASSIFIER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.info(
            "[triage] classifier exceeded %.1fs — defaulting to HEAVY",
            settings.AI_FAST_ACTION_CLASSIFIER_TIMEOUT_S,
        )
        return LABEL_HEAVY
    except LLMUnavailable as exc:
        logger.debug("[triage] %s — defaulting to HEAVY", exc)
        return LABEL_HEAVY
    except LLMError as exc:
        logger.warning(
            "[triage] classifier LLM error %s — defaulting to HEAVY",
            exc,
        )
        return LABEL_HEAVY
    except Exception:  # noqa: BLE001 - never crash the inbound
        logger.exception(
            "[triage] classifier crashed — defaulting to HEAVY"
        )
        return LABEL_HEAVY

    decision = (raw or "").strip().upper()
    for ch in ('"', "'", ".", ",", "!", "?", ":", ";"):
        decision = decision.strip(ch).strip()
    decision = decision.split()[0] if decision else ""
    decision = _LABEL_ALIASES.get(decision, decision)
    if decision not in ALL_LABELS:
        logger.info(
            "[triage] classify decision=UNRECOGNISED raw=%r for %r — "
            "treating as HEAVY",
            raw[:40] if raw else "",
            text[:80],
        )
        return LABEL_HEAVY
    logger.info(
        "[triage] classify decision=%s (raw=%r) for %r",
        decision,
        raw[:40] if raw else "",
        text[:80],
    )
    return decision


# ---------------------------------------------------------------------------
# Action-path system prompt
# ---------------------------------------------------------------------------


def _resolve_family_timezone(db: Session, family_id: int) -> str:
    family = db.get(models.Family, family_id)
    if family is not None and getattr(family, "timezone", None):
        return family.timezone
    return "America/New_York"


def _build_fast_action_system_prompt(
    db: Session,
    *,
    intent: str,
    family_id: int,
    speaker_person_id: Optional[int],
    surface_label: str,
    requestor_is_admin: bool = False,
) -> str:
    """Slim system prompt for the per-intent action micro-loop.

    Deliberately omits the RAG dump, schema crib, and the heavy agent's
    21-tool selection encyclopedia. Keeps persona, safety wrapper,
    speaker scope (so authz at the tool layer still works), today's
    date in the family timezone, and a per-intent tool nudge.
    """
    family = db.get(models.Family, family_id)
    assistant_name = (
        family.assistant.assistant_name
        if family and family.assistant
        else "Avi"
    )
    family_name = family.family_name if family else None
    tz_name = _resolve_family_timezone(db, family_id)
    try:
        tz_info = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz_info = ZoneInfo("America/New_York")
        tz_name = "America/New_York"
    now_local = datetime.now(tz_info)
    today_block = (
        "--- Time / date context ---\n"
        f"Family timezone: {tz_name}\n"
        f"Current local datetime: {now_local.isoformat(timespec='minutes')}\n"
        f"Today: {now_local.strftime('%A, %Y-%m-%d')}\n"
        "When the user says 'tomorrow' / 'next Monday' / '10am', "
        "translate to ISO 8601 in the family timezone before calling "
        "a tool."
    )

    persona = system_prompt_for_avi(assistant_name, family_name)
    speaker_block = authz.render_speaker_scope_block(
        authz.build_speaker_scope(
            db,
            speaker_person_id=speaker_person_id,
            family_id=family_id,
            requestor_is_admin=requestor_is_admin,
        )
    )
    intent_hint = _INTENT_HINTS.get(intent, "")
    surface_block = (
        f"--- Surface ---\n"
        f"The user reached you via {surface_label}. Keep the reply "
        "short and conversational; no Markdown, no headings, no "
        "bullet lists."
    )
    fast_path_contract = (
        "--- Fast-path scope ---\n"
        "You are the FAST-PATH agent for one specific household "
        "action. You have a tiny curated tool set — use it. Do NOT "
        "ask follow-up questions if you can reasonably default; do "
        "NOT chain more than two tool calls (one lookup, one write). "
        "If the request is OUTSIDE the scope of the tools you were "
        "given (e.g. user wants you to send an email, attach a file, "
        "do web research), reply with exactly one short sentence "
        '("Handing this to the full agent.") and STOP — the heavy '
        "agent will retry from scratch."
    )

    parts: List[str] = [persona, speaker_block, today_block]
    if intent_hint:
        parts.append("--- This turn ---\n" + intent_hint)
    parts.append(fast_path_contract)
    parts.append(surface_block)
    return prompts_mod.with_safety("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Narrow registry builder
# ---------------------------------------------------------------------------


def _build_intent_registry(
    full_registry: agent_tools.ToolRegistry,
    intent: str,
    capabilities: set[str],
) -> Tuple[agent_tools.ToolRegistry, List[str]]:
    """Sub-registry containing only the tools for ``intent``.

    Filters by capability so the model isn't tempted to call a tool
    whose backing integration is offline. Returns the sub-registry
    plus the names actually registered (for the log line).
    """
    wanted = _INTENT_TOOLS.get(intent, ())
    sub = agent_tools.ToolRegistry()
    registered: List[str] = []
    for name in wanted:
        t = full_registry.get(name)
        if t is None:
            continue
        if t.requires and not all(req in capabilities for req in t.requires):
            continue
        sub.register(t)
        registered.append(name)
    return sub, registered


# ---------------------------------------------------------------------------
# Per-intent action runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionResult:
    """Outcome of running the per-intent action micro-loop.

    ``final_text`` is the user-visible reply when the loop succeeded;
    ``None`` means "shortcut failed, fall through to the heavy agent".
    ``escalated`` is set when the fast model emitted the escalation
    sentence ("Handing this to the full agent.").
    """

    final_text: Optional[str]
    escalated: bool = False


_ESCALATION_TOKENS = (
    "handing this to the full agent",
    "handing this to the agent",
    "handing off to the agent",
    "passing to the agent",
    "passing to the full agent",
    "routing to the full agent",
)


def _looks_like_escalation(text: str) -> bool:
    low = (text or "").strip().lower()
    if len(low) > 200:
        return False
    return any(tok in low for tok in _ESCALATION_TOKENS)


async def _run_action(
    *,
    intent: str,
    user_message: str,
    family_id: int,
    person_id: Optional[int],
    assistant_id: Optional[int],
    live_session_id: Optional[int],
    surface_label: str,
    agent_task_kind: str = "fast_action",
    requestor_is_admin: bool = False,
) -> ActionResult:
    """Drive the per-intent micro-loop. Returns ``None`` on every failure.

    Creates its own :class:`models.AgentTask` row so the kanban / audit
    plumbing is consistent across surfaces — the fast path is genuinely
    tool-executing work and needs the same paper trail the heavy agent
    leaves.
    """
    text = (user_message or "").strip()
    if not text:
        return ActionResult(final_text=None)
    settings = get_settings()
    if not settings.AI_FAST_ACTION_SHORTCUT_ENABLED:
        return ActionResult(final_text=None)

    if intent not in _INTENT_TOOLS:
        logger.warning(
            "[triage] action runner called with unknown intent %r — bailing",
            intent,
        )
        return ActionResult(final_text=None)

    model_tag = llm.resolve_model(settings.AI_FAST_ACTION_MODEL, fast=True)
    max_steps = _INTENT_MAX_STEPS.get(intent, settings.AI_FAST_ACTION_MAX_STEPS)

    with SessionLocal() as db:
        try:
            registry = agent_tools.build_default_registry()
            capabilities = agent_tools.detect_capabilities(db, assistant_id)
            sub_registry, registered_tools = _build_intent_registry(
                registry, intent, capabilities
            )
            if not registered_tools:
                logger.info(
                    "[triage] intent=%s has no tools available "
                    "(missing capabilities=%s) — falling through to heavy",
                    intent,
                    sorted(
                        {
                            req
                            for name in _INTENT_TOOLS[intent]
                            for t in [registry.get(name)]
                            if t is not None
                            for req in t.requires
                        }
                    ),
                )
                return ActionResult(final_text=None)

            system_prompt = _build_fast_action_system_prompt(
                db,
                intent=intent,
                family_id=family_id,
                speaker_person_id=person_id,
                surface_label=surface_label,
                requestor_is_admin=requestor_is_admin,
            )
            task = agent_loop.create_task(
                db,
                family_id=family_id,
                live_session_id=live_session_id,
                person_id=person_id,
                kind=agent_task_kind,
                input_text=text,
                model=model_tag,
            )
            db.commit()
            task_id = task.agent_task_id
        except Exception:  # noqa: BLE001 - shortcut never breaks caller
            logger.exception(
                "[triage] action setup failed (intent=%s) — falling through",
                intent,
            )
            return ActionResult(final_text=None)

    logger.info(
        "[triage] action start intent=%s model=%s family_id=%s "
        "task=%s person_id=%s surface=%s max_steps=%d tools=%s",
        intent,
        model_tag,
        family_id,
        task_id,
        person_id,
        surface_label,
        max_steps,
        registered_tools,
    )
    started = time.monotonic()

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                agent_drain.drain_agent_sync,
                task_id=task_id,
                family_id=family_id,
                assistant_id=assistant_id,
                person_id=person_id,
                system_prompt=system_prompt,
                history=[],
                user_message=text,
                registry=sub_registry,
                capabilities=capabilities,
                extra_run_kwargs={
                    "model_override": model_tag,
                    "max_steps": max_steps,
                    "requestor_is_admin": requestor_is_admin,
                },
            ),
            timeout=(
                settings.AI_FAST_ACTION_TURN_TIMEOUT_S * max_steps + 10.0
            ),
        )
    except asyncio.TimeoutError:
        logger.info(
            "[triage] action timeout intent=%s task=%s — "
            "falling through to heavy agent",
            intent,
            task_id,
        )
        return ActionResult(final_text=None)
    except Exception:  # noqa: BLE001 - shortcut never breaks caller
        logger.exception(
            "[triage] action crashed intent=%s task=%s — falling through",
            intent,
            task_id,
        )
        return ActionResult(final_text=None)

    duration_ms = int((time.monotonic() - started) * 1000)
    final = (result.final_text or "").strip()

    if result.error_text and not final:
        logger.info(
            "[triage] action failed intent=%s task=%s duration_ms=%d "
            "error=%s — falling through",
            intent,
            task_id,
            duration_ms,
            result.error_text[:120],
        )
        return ActionResult(final_text=None)

    if _looks_like_escalation(final):
        logger.info(
            "[triage] action escalated intent=%s task=%s duration_ms=%d "
            "reply=%r — falling through",
            intent,
            task_id,
            duration_ms,
            final[:80],
        )
        return ActionResult(final_text=None, escalated=True)

    if not final:
        logger.info(
            "[triage] action produced empty reply intent=%s task=%s "
            "duration_ms=%d — falling through",
            intent,
            task_id,
            duration_ms,
        )
        return ActionResult(final_text=None)

    logger.info(
        "[triage] action done intent=%s task=%s duration_ms=%d "
        "reply_chars=%d",
        intent,
        task_id,
        duration_ms,
        len(final),
    )
    return ActionResult(final_text=final)


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageResult:
    """What the triage decided and (when applicable) the reply text.

    ``label`` is always one of :data:`ALL_LABELS`. ``reply`` is the
    user-visible text when a shortcut handled the turn, else ``None``
    — in which case the caller must run the heavy agent. ``used_path``
    names the shortcut that produced the reply (``web_search`` /
    ``family_qa`` / ``fast_action``) for audit annotations; ``None``
    when ``label == HEAVY`` or the shortcut bailed mid-flight.
    """

    label: str
    reply: Optional[str] = None
    used_path: Optional[str] = None
    intent: Optional[str] = None
    used_model: Optional[str] = None


async def try_triage(
    user_message: str,
    *,
    family_id: int,
    person_id: Optional[int],
    assistant_id: Optional[int],
    live_session_id: Optional[int] = None,
    surface_label: str = "chat",
    agent_task_kind: str = "fast_action",
    requestor_is_admin: bool = False,
    # Per-surface knobs for which shortcuts the caller is willing to use
    allow_web_search: bool = True,
    allow_family_qa: bool = True,
    allow_action: bool = True,
    # Family-QA needs household identity strings. When the caller doesn't
    # provide them we look them up from family_id.
    family_qa_db: Optional[Session] = None,
    family_qa_assistant_name: Optional[str] = None,
    family_qa_family_name: Optional[str] = None,
) -> TriageResult:
    """Classify the message and dispatch to the winning shortcut.

    Wraps the whole flow in defensive ``except`` so callers don't have
    to duplicate guards — every failure mode (Ollama down, classifier
    crash, shortcut runtime crash) collapses to ``TriageResult(label=
    HEAVY)`` so the caller can transparently fall through to the
    heavy agent.

    Per-surface gates (``allow_*``) let callers opt out of individual
    paths. Example: the live chat surface skips ``FAMILY_QA`` for
    admins and sensitive-identifier asks because the fast tier's
    training doesn't include the admin-bypass examples and would
    refuse a question the heavy agent (with its tool-layer privacy
    gate) can answer correctly.
    """
    text = (user_message or "").strip()
    if not text:
        return TriageResult(label=LABEL_HEAVY)

    try:
        label = await classify_triage(text)
    except Exception:  # noqa: BLE001 - belt and suspenders
        logger.exception("[triage] classify_triage crashed — defaulting HEAVY")
        return TriageResult(label=LABEL_HEAVY)

    settings = get_settings()

    # ------ WEB shortcut ------------------------------------------------
    if label == LABEL_WEB:
        if not allow_web_search or not settings.AI_WEB_SEARCH_SHORTCUT_ENABLED:
            return TriageResult(label=LABEL_HEAVY)
        try:
            answer = await web_search_shortcut.run(text)
        except Exception:  # noqa: BLE001
            logger.exception("[triage] web shortcut crashed — falling through")
            return TriageResult(label=LABEL_HEAVY)
        if not answer:
            return TriageResult(label=LABEL_HEAVY)
        return TriageResult(
            label=LABEL_WEB,
            reply=answer,
            used_path="web_search",
            used_model="gemini_grounded",
        )

    # ------ FAMILY_QA shortcut -----------------------------------------
    if label == LABEL_FAMILY_QA:
        if (
            not allow_family_qa
            or not settings.AI_FAMILY_QA_SHORTCUT_ENABLED
        ):
            return TriageResult(label=LABEL_HEAVY)
        # Resolve household identity for the prompt. If the caller
        # passed in an open Session use it; otherwise mint our own.
        own_db = family_qa_db is None
        db = family_qa_db or SessionLocal()
        try:
            assistant_name = family_qa_assistant_name
            family_name = family_qa_family_name
            if assistant_name is None or family_name is None:
                fam = db.get(models.Family, family_id)
                if fam is not None:
                    family_name = family_name or fam.family_name
                    if fam.assistant is not None:
                        assistant_name = (
                            assistant_name or fam.assistant.assistant_name
                        )
            assistant_name = assistant_name or "Avi"
            try:
                answer = await family_qa_router.run(
                    text,
                    db=db,
                    family_id=family_id,
                    recognized_person_id=person_id,
                    assistant_name=assistant_name,
                    family_name=family_name,
                    requestor_is_admin=requestor_is_admin,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[triage] family_qa shortcut crashed — falling through"
                )
                return TriageResult(label=LABEL_HEAVY)
        finally:
            if own_db:
                db.close()
        if not answer:
            return TriageResult(label=LABEL_HEAVY)
        return TriageResult(
            label=LABEL_FAMILY_QA,
            reply=answer,
            used_path="family_qa",
            used_model=family_qa_router._answer_model(),
        )

    # ------ ACTION intents ---------------------------------------------
    if label in ACTION_LABELS:
        if not allow_action:
            return TriageResult(label=LABEL_HEAVY)
        try:
            action = await _run_action(
                intent=label,
                user_message=text,
                family_id=family_id,
                person_id=person_id,
                assistant_id=assistant_id,
                live_session_id=live_session_id,
                surface_label=surface_label,
                agent_task_kind=agent_task_kind,
                requestor_is_admin=requestor_is_admin,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[triage] action runner crashed (intent=%s) — falling through",
                label,
            )
            return TriageResult(label=LABEL_HEAVY)
        if not action.final_text:
            return TriageResult(label=LABEL_HEAVY)
        return TriageResult(
            label=label,
            reply=action.final_text,
            used_path="fast_action",
            intent=label,
            used_model=llm.resolve_model(
                settings.AI_FAST_ACTION_MODEL, fast=True
            ),
        )

    return TriageResult(label=LABEL_HEAVY)


def try_triage_sync(
    user_message: str,
    *,
    family_id: int,
    person_id: Optional[int],
    assistant_id: Optional[int],
    live_session_id: Optional[int] = None,
    surface_label: str = "chat",
    agent_task_kind: str = "fast_action",
    requestor_is_admin: bool = False,
    allow_web_search: bool = True,
    allow_family_qa: bool = True,
    allow_action: bool = True,
) -> TriageResult:
    """Synchronous wrapper for the SMS / WhatsApp / Telegram / email flows.

    Those surfaces run inside ``asyncio.to_thread`` worker threads with
    no ambient loop, so they need a sync entry point that owns its own
    loop. Every failure path returns ``TriageResult(label=HEAVY)``.
    """
    if not (user_message or "").strip():
        return TriageResult(label=LABEL_HEAVY)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            try_triage(
                user_message,
                family_id=family_id,
                person_id=person_id,
                assistant_id=assistant_id,
                live_session_id=live_session_id,
                surface_label=surface_label,
                agent_task_kind=agent_task_kind,
                requestor_is_admin=requestor_is_admin,
                allow_web_search=allow_web_search,
                allow_family_qa=allow_family_qa,
                allow_action=allow_action,
            )
        )
    except Exception:  # noqa: BLE001 - triage never breaks the inbound
        logger.exception(
            "[triage] try_triage_sync crashed — defaulting to HEAVY"
        )
        return TriageResult(label=LABEL_HEAVY)
    finally:
        loop.close()


__all__ = [
    "ACTION_LABELS",
    "ALL_LABELS",
    "ActionResult",
    "LABEL_CALENDAR_ADD",
    "LABEL_CALENDAR_LIST",
    "LABEL_FAMILY_QA",
    "LABEL_HEAVY",
    "LABEL_TASK_ADD",
    "LABEL_TASK_LIST",
    "LABEL_TASK_UPDATE",
    "LABEL_EMAIL_SEND",
    "LABEL_WEB",
    "TriageResult",
    "classify_triage",
    "try_triage",
    "try_triage_sync",
]
