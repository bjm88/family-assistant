"""Chat + greet endpoints for the live AI assistant.

* ``GET  /api/aiassistant/chat/status`` — surfaces Ollama reachability
  and which model is configured.
* ``POST /api/aiassistant/greet``       — non-streaming, returns a short
  greeting targeted at a specific person the camera just recognized.
  Uses the RAG builder to describe that person to the LLM.
* ``POST /api/aiassistant/chat``        — streaming Server-Sent-Events
  chat. The client posts the whole conversation; we stream LLM tokens
  back plus an optional RAG block derived from an (optional)
  ``recognized_person_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..auth import (
    CurrentUser,
    require_family_member,
    require_user,
)
from ..ai import agent as agent_loop
from ..ai import assistants as _assistants
from ..ai import chat_prompts
from ..ai import fast_ack
from ..ai import fast_action_router
from ..ai import llm, ollama
from ..ai import planner as ai_planner
from ..ai import prompts
from ..ai import rag
from ..ai import schema_catalog
from ..ai import sensitive_intent
from ..ai import session as live_session
from ..ai import sql_tool
from ..ai import tools as agent_tools
from ..config import get_settings
from ..db import SessionLocal, get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["ai_chat"])


# ---------- Schemas -------------------------------------------------------


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class GreetRequest(BaseModel):
    family_id: int
    person_id: int
    # Live-session context. When provided, the server upserts a
    # participant row, enforces the greet-once rule via
    # ``greeted_already``, and logs the greeting to the transcript.
    live_session_id: Optional[int] = None
    # Kept for backwards compatibility with any early caller; the greet
    # path is now pure template and never hits the LLM. Use /followup
    # for the goal-based question.
    include_goal_question: bool = False


class GreetResponse(BaseModel):
    family_id: int
    person_id: int
    # When the participant was already greeted in this session we
    # return an empty greeting string and ``skipped=True`` so the
    # client knows to stay silent.
    greeting: str
    skipped: bool = False
    skipped_reason: Optional[str] = None
    # "template" for the instant path, "<model>" when the LLM was used.
    used_model: str
    context_preview: str


class FollowupRequest(BaseModel):
    family_id: int
    person_id: int
    live_session_id: Optional[int] = None


class FollowupResponse(BaseModel):
    family_id: int
    person_id: int
    question: str
    goal_name: Optional[str]
    used_model: str


class ChatRequest(BaseModel):
    family_id: int
    messages: List[ChatMessage]
    recognized_person_id: Optional[int] = None
    live_session_id: Optional[int] = None


class StatusResponse(BaseModel):
    host: str
    model: str
    available: bool
    model_pulled: bool
    installed_models: List[str]
    error: Optional[str] = None


# ---------- Helpers -------------------------------------------------------


def _load_assistant(db: Session, family_id: int) -> tuple[str, Optional[str]]:
    family = db.get(models.Family, family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")
    assistant = family.assistant
    name = assistant.assistant_name if assistant else "Avi"
    return name, family.family_name


_assistant_id_for = _assistants.assistant_id_for_family


# ---------- Status --------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
async def status(_: CurrentUser = Depends(require_user)) -> StatusResponse:
    if llm.provider() == "grok":
        key_ok = bool(get_settings().grok_api_key)
        return StatusResponse(
            host=get_settings().GROK_API_BASE,
            model=llm._model(),
            available=key_ok,
            model_pulled=key_ok,
            installed_models=[llm._model(), llm.fast_model()],
            error=None if key_ok else "GROK_API_KEY is not set",
        )
    info = await ollama.health()
    return StatusResponse(**info)  # type: ignore[arg-type]


# ---------- Warmup --------------------------------------------------------


class WarmupResponse(BaseModel):
    """Outcome of a model-warmup request.

    ``heavy`` / ``fast`` are the configured model tags (so the UI
    can display *what* it warmed). ``heavy_loaded`` / ``fast_loaded``
    are the boolean results of the underlying ``/api/generate``
    ping — ``False`` means Ollama was down, the model wasn't pulled,
    or the call exceeded its timeout. Errors are swallowed by
    :func:`api.ai.ollama.warmup_model` so this endpoint never 5xxs.
    """

    heavy: str
    fast: str
    heavy_loaded: bool
    fast_loaded: bool


@router.post("/warmup", response_model=WarmupResponse)
async def warmup(_: CurrentUser = Depends(require_user)) -> WarmupResponse:
    """Pre-load both Gemma models on demand from the live page.

    The lifespan startup task already warms the models when the
    server boots (`api.main._lifespan` -> `_ollama_warmup`), but
    Ollama unloads anything idle longer than the configured
    ``keep_alive`` window. After lunch / overnight, the first chat
    of the next session would otherwise pay the 3–4 s cold-load
    cost again — exactly the failure mode the live-chat fast-ack
    was designed to avoid.

    Calling this endpoint when the AI Assistant page mounts
    (and again whenever the user returns to it) re-pings both
    models with ``keep_alive="1h"`` so they're hot before the
    user even types. The work runs in parallel and the response
    blocks until both pings finish, so the UI can show a clear
    "models warm" indicator.

    No DB session, no auth — pure Ollama plumbing.
    """
    heavy_tag = llm._model()
    fast_tag = llm.fast_model()
    if not llm.ollama_warmup_enabled():
        return WarmupResponse(
            heavy=heavy_tag,
            fast=fast_tag,
            heavy_loaded=True,
            fast_loaded=True,
        )
    heavy_loaded, fast_loaded = await asyncio.gather(
        ollama.warmup_model(get_settings().AI_OLLAMA_MODEL),
        ollama.warmup_model(get_settings().AI_OLLAMA_FAST_MODEL),
        return_exceptions=False,
    )
    return WarmupResponse(
        heavy=heavy_tag,
        fast=fast_tag,
        heavy_loaded=bool(heavy_loaded),
        fast_loaded=bool(fast_loaded),
    )


# ---------- Greet ---------------------------------------------------------


def _display_name(person: models.Person) -> str:
    # For the out-loud greeting we want Avi to use the person's main
    # name (first_name) rather than a household nickname. Falls back to
    # preferred_name only if first_name is missing.
    return person.first_name or person.preferred_name or f"Person {person.person_id}"


@router.post("/greet", response_model=GreetResponse)
async def greet(
    payload: GreetRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> GreetResponse:
    """Instant, no-LLM greeting so Avi can *start talking* within a few
    hundred milliseconds of spotting a face. The contextual question
    ("how's your diet going?") comes from ``/followup`` and is spoken
    after the greeting finishes playing.

    Session behaviour
    -----------------
    When ``live_session_id`` is supplied:

    * a :class:`LiveSessionParticipant` row is upserted for the
      ``(session, person)`` pair,
    * ``greeted_already`` is flipped from ``False`` to ``True`` in a
      single conditional UPDATE (see :func:`live_session.mark_greeted`),
    * if the participant had already been greeted in this session we
      return ``skipped=True`` with an empty greeting so the client
      stays silent,
    * on a successful greeting we log an assistant-role
      :class:`LiveSessionMessage` so the history view is complete.
    """
    require_family_member(payload.family_id, request)
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    context = rag.build_person_context(db, person)

    # Fast path — no session tracking, pre-session-feature behaviour.
    if payload.live_session_id is None:
        return GreetResponse(
            family_id=payload.family_id,
            person_id=payload.person_id,
            greeting=f"Hi {_display_name(person)}, how can I help you?",
            used_model="template",
            context_preview=context,
        )

    session = db.get(models.LiveSession, payload.live_session_id)
    if session is None or session.family_id != payload.family_id:
        raise HTTPException(
            status_code=404, detail="Live session not found for this family"
        )

    participant = live_session.upsert_participant(
        db, session, person_id=person.person_id
    )

    # If the user is RECENTLY mid-conversation — i.e. typed something
    # to Avi within the last `AI_GREET_SUPPRESS_RECENT_CHAT_SECONDS`
    # — a sudden "Hi Ben!" is jarring. Mark them greeted silently and
    # stay quiet. We use the same atomic CAS so concurrent /greet
    # calls still resolve to a single winner; only the caller that
    # flipped False→True needs to do any work, and even that work
    # becomes a no-op return here.
    #
    # The check is *time-windowed* rather than "any chat history
    # ever". A live session can stay open for 30 minutes of idle
    # time (see AI_LIVE_SESSION_IDLE_MINUTES); without the window
    # bound, one chat message would silently kill every subsequent
    # face-rec greeting for the rest of that 30-minute window —
    # including after the user wandered away from the camera and
    # came back, or refreshed the page mid-session. That regression
    # was the bug behind the "live page stopped greeting me again"
    # report on 2026-04-20; integration test
    # ``test_face_greet.py::test_greet_after_old_chat_does_not_skip``
    # locks the new behaviour in.
    suppress_seconds = int(
        get_settings().AI_GREET_SUPPRESS_RECENT_CHAT_SECONDS
    )
    has_recent_chat = False
    if suppress_seconds > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=suppress_seconds)
        has_recent_chat = (
            db.query(models.LiveSessionMessage)
            .filter(
                models.LiveSessionMessage.live_session_id
                == session.live_session_id,
                models.LiveSessionMessage.role.in_(("user", "assistant")),
                models.LiveSessionMessage.created_at >= cutoff,
            )
            .filter(
                (models.LiveSessionMessage.meta.is_(None))
                | (models.LiveSessionMessage.meta["kind"].as_string() == "chat")
            )
            .first()
            is not None
        )
    if has_recent_chat:
        live_session.mark_greeted(db, participant)
        logger.info(
            "[greet] suppressed family=%s person=%s session=%s "
            "reason=session_already_active window_s=%d",
            payload.family_id,
            payload.person_id,
            session.live_session_id,
            suppress_seconds,
        )
        return GreetResponse(
            family_id=payload.family_id,
            person_id=payload.person_id,
            greeting="",
            skipped=True,
            skipped_reason="session_already_active",
            used_model="template",
            context_preview=context,
        )

    # Atomic CAS: only the caller that flips False→True actually greets.
    greeted_now = live_session.mark_greeted(db, participant)
    if not greeted_now:
        # Another request already said hi to them in this session —
        # surface the reason so the client log is informative.
        return GreetResponse(
            family_id=payload.family_id,
            person_id=payload.person_id,
            greeting="",
            skipped=True,
            skipped_reason="already_greeted_in_session",
            used_model="template",
            context_preview=context,
        )

    greeting = f"Hi {_display_name(person)}, how can I help you?"
    live_session.log_message(
        db,
        session,
        role="assistant",
        content=greeting,
        meta={
            "kind": "greeting",
            "person_id": person.person_id,
            "used_model": "template",
        },
    )
    logger.info(
        "[greet] sent family=%s person=%s session=%s text_chars=%d",
        payload.family_id,
        payload.person_id,
        session.live_session_id,
        len(greeting),
    )
    return GreetResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        greeting=greeting,
        used_model="template",
        context_preview=context,
    )


# ---------- Follow-up question (LLM) -------------------------------------


@router.post("/followup", response_model=FollowupResponse)
async def followup(
    payload: FollowupRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> FollowupResponse:
    """LLM-generated, one-sentence follow-up aimed at the person's most
    salient goal (or a generic "how are you?" when they haven't set one).
    Runs asynchronously while the instant greeting is already playing.
    """
    require_family_member(payload.family_id, request)
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    context = rag.build_person_context(db, person)
    goal = rag.pick_goal_for_question(person)
    has_interests = bool((person.interests_and_activities or "").strip())
    has_notes = bool((person.notes or "").strip())

    # Tell the model exactly what raw material is in the context block so
    # it doesn't have to guess. The "anchor" hint nudges it to pick the
    # most personal thing rather than asking another generic question.
    available_bits: List[str] = []
    if goal is not None:
        available_bits.append(f"their goal \"{goal.goal_name}\"")
    if has_interests:
        available_bits.append("one of their listed interests / hobbies")
    if has_notes:
        available_bits.append("something specific from their notes")
    if not available_bits:
        # Truly nothing personal known. Ask something gentle, but still
        # not the same boring sentence every time.
        anchor_hint = (
            "We don't have anything specific about them yet — keep the "
            "question open and inviting (e.g. ask what they've been "
            "enjoying lately, or what's on their mind today). Vary the "
            "wording so it doesn't sound templated."
        )
    else:
        anchor_hint = (
            "Anchor the question in ONE specific detail from the context "
            "above — for example "
            + ", or ".join(available_bits)
            + ". Reference the detail concretely (use the actual goal "
            "name, hobby, or note phrase) so it's clear you remember "
            "them. Do NOT ask a generic 'how was your day' or 'how are "
            "you doing today' — those are banned."
        )

    task = (
        "Ask ONE short, warm, specific follow-up question to keep the "
        "conversation going. Single sentence only, conversational, no "
        "preamble.\n\n"
        + anchor_hint
    )

    prompt = (
        "You just greeted this family member and want to keep the "
        "conversation going.\n\n"
        f"--- Who you are talking to ---\n{context}\n\n"
        f"--- Your task ---\n{task}\n\n"
        "Reply with only the spoken question — no preamble, no quotes, "
        "no restating their name, no leading 'Sure!' or 'Great!'."
    )

    logger.info(
        "[followup] person_id=%s name=%r goal=%r has_interests=%s has_notes=%s",
        person.person_id,
        person.preferred_name or person.first_name,
        goal.goal_name if goal else None,
        has_interests,
        has_notes,
    )
    # Dump the raw context block so we can verify the goals + interests
    # actually made it in. Truncated to keep the log readable.
    _ctx_preview = context.replace("\n", " | ")
    logger.info(
        "[followup] context_preview=%s",
        _ctx_preview[:600] + ("…" if len(_ctx_preview) > 600 else ""),
    )

    system = prompts.with_safety(
        ollama.system_prompt_for_avi(assistant_name, family_name)
    )

    # One-sentence follow-up — perfect fit for the lightweight model.
    # The fallback wrapper transparently uses the main chat model if
    # the fast tag isn't pulled yet.
    try:
        question, followup_model = await llm.generate_with_fallback(
            prompt,
            primary_model=llm.fast_model(),
            system=system,
            temperature=0.8,
            max_tokens=120,
        )
    except llm.LLMUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=f"LLM is not available: {e}",
        )
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    raw_question = (question or "").strip()
    logger.info(
        "[followup] model=%s raw_response=%r", followup_model, raw_question
    )

    # Reject the lazy fallback even if the model actually produced it.
    # Without this guard a model that lapses into "How's your day going?"
    # would slip through and look exactly like our hard-coded fallback.
    GENERIC_PHRASES = (
        "how's your day",
        "how is your day",
        "how are you doing today",
        "how are you today",
    )
    if not raw_question or any(
        g in raw_question.lower() for g in GENERIC_PHRASES
    ):
        # Build a deterministic, personal fallback from the structured
        # context so we never speak the boring sentence.
        if goal is not None:
            final_question = (
                f"How is it going with your goal to {goal.goal_name}?"
            )
        elif has_interests:
            interest = (person.interests_and_activities or "").strip()
            # Take the first comma-separated chunk so we name something
            # specific instead of reading the whole list aloud.
            first_interest = interest.split(",")[0].strip().rstrip(".")
            if first_interest:
                final_question = (
                    f"Have you done any {first_interest} lately?"
                )
            else:
                final_question = "What have you been up to lately?"
        else:
            final_question = "What's been on your mind today?"
        logger.info(
            "[followup] substituted_fallback=%r (model produced %r)",
            final_question,
            raw_question,
        )
    else:
        final_question = raw_question

    # Log into the transcript when we have a session, so the history
    # view can replay the full Avi → human → Avi conversation rhythm.
    if payload.live_session_id is not None:
        session = db.get(models.LiveSession, payload.live_session_id)
        if session is not None and session.family_id == payload.family_id:
            live_session.log_message(
                db,
                session,
                role="assistant",
                content=final_question,
                meta={
                    "kind": "followup",
                    "person_id": person.person_id,
                    "goal_name": goal.goal_name if goal else None,
                    "used_model": followup_model,
                },
            )

    return FollowupResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        question=final_question,
        goal_name=goal.goal_name if goal else None,
        used_model=followup_model,
    )


# ---------- Direct SQL (debug + LLM tool) --------------------------------


class SqlRequest(BaseModel):
    family_id: int
    query: str = Field(..., min_length=1, max_length=4000)
    max_rows: int = Field(200, ge=1, le=1000)


class SqlResponse(BaseModel):
    sql: str
    columns: List[str]
    rows: List[dict]
    row_count: int
    truncated: bool


@router.post("/sql", response_model=SqlResponse)
def run_sql(
    payload: SqlRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> SqlResponse:
    """Execute a sandboxed read-only SELECT.

    Used by the chat planner under the hood and exposed directly so
    operators can poke at the schema with the same allow-list /
    timeouts the LLM operates under.
    """
    require_family_member(payload.family_id, request)
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    try:
        result = sql_tool.run_safe_query(
            db,
            payload.query,
            family_id=payload.family_id,
            max_rows=payload.max_rows,
        )
    except sql_tool.SqlToolError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SqlResponse(**result.to_dict())


# ---------- Chat stream ---------------------------------------------------


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Streaming chat endpoint, driven by the unified triage router.

    Pipeline (matches the SMS / WhatsApp / Telegram surfaces in spirit
    so the "Avi acknowledges instantly" UX contract is identical
    everywhere):

    1.  **Instant heuristic ack.** First byte of the SSE stream is
        ``{"type": "fast_ack", "text": ..., "fast_ack_kind":
        "heuristic"}`` — pure Python, no LLM call, ~10 µs. The user
        sees Avi confirm the message before any classifier runs.
    2.  **Unified triage** (:func:`api.ai.fast_action_router.try_triage`).
        One fast-model call decides between WEB / FAMILY_QA /
        per-intent action / HEAVY. Bounded by
        :setting:`AI_FAST_ACTION_CLASSIFIER_TIMEOUT_S`; every failure
        path returns ``label=HEAVY`` so the heavy agent runs.
    3.  **Shortcut path** — if triage handled the turn, stream the
        reply as ``{"task_id": null}`` + ``{"delta": ...}`` +
        ``{"type": "task_completed", ...}`` + ``{"done": true}``.
        No AgentTask row is created (per the minimal-audit choice
        for WEB / FAMILY_QA; the action runner creates its own
        internally).
    4.  **Heavy path** — :func:`_heavy_agent_stream` takes over.
        Creates the AgentTask, emits its ``task_id``, runs the
        full :func:`api.ai.agent.run_agent` loop, optionally races
        an e2b "upgrade" ack against the first model delta.

    Either path closes with ``{"done": true}`` so the UI's
    streaming-indicator teardown is uniform.

    When a ``live_session_id`` is provided the server records the
    user message (in this handler, before the stream) and the final
    assistant reply (inside the stream, in a fresh DB session). The
    instant heuristic ack and any e2b upgrade ack are also logged
    with ``meta.kind="live_fast_ack"`` so the History view replays
    them in order.
    """
    user = require_family_member(payload.family_id, request)
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    # Members can't impersonate other people on the live chat — pin
    # ``recognized_person_id`` to the logged-in user's own person row
    # so RAG / transcript / agent context all match the human at
    # the keyboard. Admins keep the spoofing power for testing.
    if not user.is_admin and user.person_id is not None:
        payload.recognized_person_id = user.person_id

    # ---------- Setup (fast pre-stream prep) ---------------------------
    # Everything here is cheap DB reads that we want done while the
    # request-scoped session is still open. The actual classifier,
    # shortcut runner, and heavy agent all run inside the SSE stream
    # so the user sees the heuristic ack on byte one.
    settings = get_settings()
    latest_user_for_shortcut = next(
        (m.content for m in reversed(payload.messages) if m.role == "user"),
        "",
    ).strip()
    history = [m.model_dump() for m in payload.messages]
    latest_user_content = next(
        (m["content"] for m in reversed(history) if m["role"] == "user"),
        "",
    )

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    assistant_id = _assistant_id_for(db, payload.family_id)
    requestor_is_admin = bool(user.is_admin)

    # Sensitive-identifier asks (SSN, VIN, account number, passport,
    # DL, policy number) bypass FAMILY_QA so the deterministic
    # household-privacy gate in ``secrets.py`` makes the call. The
    # action-intent runner is fine because none of its tools touch
    # encrypted identifiers.
    is_sensitive_ask = sensitive_intent.is_sensitive_identifier_ask(
        latest_user_for_shortcut
    )
    if is_sensitive_ask:
        logger.info(
            "[orch] surface=live_chat shortcut=family_qa skipped "
            "reason=sensitive_identifier_ask family_id=%s msg=%r — "
            "routing to heavy agent so authz runs at the tool layer",
            payload.family_id,
            latest_user_for_shortcut[:80],
        )

    # Log the inbound user message up-front so it's persisted even if
    # the stream crashes partway through. We need the LiveSession to
    # exist + belong to this family — verify both here.
    logged_session_id: Optional[int] = None
    if payload.live_session_id is not None:
        session = db.get(models.LiveSession, payload.live_session_id)
        if session is not None and session.family_id == payload.family_id:
            logged_session_id = session.live_session_id
            if latest_user_content:
                live_session.log_message(
                    db,
                    session,
                    role="user",
                    content=latest_user_content,
                    person_id=payload.recognized_person_id,
                    meta={"kind": "chat"},
                )
                db.commit()

    # Best display-name for the speaker so the e2b ack prompt can
    # personalise (*"Looking up your calendar..."*) when known.
    sender_display_name: Optional[str] = None
    if payload.recognized_person_id is not None:
        speaker = db.get(models.Person, payload.recognized_person_id)
        if speaker is not None and speaker.family_id == payload.family_id:
            sender_display_name = (
                speaker.preferred_name or speaker.first_name
            )

    # Snapshot mutables we need inside the generator closure (the
    # request-scoped ``db`` is closed once the StreamingResponse
    # returns; everything heavy below opens its own SessionLocal).
    payload_family_id = payload.family_id
    payload_person_id = payload.recognized_person_id
    payload_session_id = payload.live_session_id
    allow_family_qa_for_caller = (
        not is_sensitive_ask and not requestor_is_admin
    )

    async def event_stream():
        # 1. Instant heuristic ack — pure Python, ~10 µs. The user
        #    sees Avi acknowledge the message before any LLM call.
        emitted_fast_ack: Optional[str] = None
        if settings.AI_FAST_ACK_ENABLED and latest_user_content:
            instant = fast_ack.heuristic_ack(latest_user_content)
            if instant:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "fast_ack",
                            "text": instant,
                            "fast_ack_kind": "heuristic",
                        }
                    )
                    + "\n\n"
                )
                emitted_fast_ack = instant
                logger.info(
                    "[orch] surface=live_chat instant_ack family_id=%s "
                    "session=%s text=%r",
                    payload_family_id,
                    payload_session_id,
                    instant,
                )

        # 2. Unified triage. One fast-model call decides between
        #    WEB / FAMILY_QA / one of the action intents / HEAVY.
        #    Bounded by AI_FAST_ACTION_CLASSIFIER_TIMEOUT_S — every
        #    failure path returns label=HEAVY so we fall through to
        #    the heavy agent cleanly.
        try:
            triage = await fast_action_router.try_triage(
                latest_user_for_shortcut,
                family_id=payload_family_id,
                person_id=payload_person_id,
                assistant_id=assistant_id,
                live_session_id=logged_session_id,
                surface_label="live chat",
                agent_task_kind="chat",
                requestor_is_admin=requestor_is_admin,
                allow_family_qa=allow_family_qa_for_caller,
                family_qa_assistant_name=assistant_name,
                family_qa_family_name=family_name,
            )
        except Exception:  # noqa: BLE001 - triage must never crash the stream
            logger.exception("[orch] surface=live_chat triage crashed")
            triage = fast_action_router.TriageResult(
                label=fast_action_router.LABEL_HEAVY
            )

        # 3a. Shortcut won — stream the reply and persist the
        #     transcript rows in a fresh DB session.
        if triage.reply:
            logger.info(
                "[orch] surface=live_chat path=%s family_id=%s session=%s "
                "reply_chars=%d msg=%r (skipping heavy agent)",
                triage.used_path,
                payload_family_id,
                payload_session_id,
                len(triage.reply),
                latest_user_for_shortcut[:80],
            )
            yield f"data: {json.dumps({'task_id': None})}\n\n"
            yield f"data: {json.dumps({'delta': triage.reply})}\n\n"
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "task_completed",
                        "task_id": None,
                        "summary": triage.reply,
                        "shortcut": triage.used_path,
                    }
                )
                + "\n\n"
            )
            if logged_session_id is not None:
                _log_shortcut_transcript(
                    live_session_id=logged_session_id,
                    family_id=payload_family_id,
                    recognized_person_id=payload_person_id,
                    fast_ack_text=emitted_fast_ack,
                    final_text=triage.reply,
                    shortcut_name=triage.used_path or "fast",
                    used_model=triage.used_model or "",
                )
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        # 3b. HEAVY path — build the full system prompt, create the
        #     AgentTask, run the agent loop with the e2b upgrade ack
        #     racing against the first model delta.
        async for chunk in _heavy_agent_stream(
            family_id=payload_family_id,
            recognized_person_id=payload_person_id,
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            family_name=family_name,
            requestor_is_admin=requestor_is_admin,
            sender_display_name=sender_display_name,
            logged_session_id=logged_session_id,
            history=history,
            latest_user_content=latest_user_content,
            emitted_fast_ack_already=emitted_fast_ack,
            settings=settings,
        ):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Heavy-agent stream helper ------------------------------------


async def _heavy_agent_stream(
    *,
    family_id: int,
    recognized_person_id: Optional[int],
    assistant_id: Optional[int],
    assistant_name: str,
    family_name: Optional[str],
    requestor_is_admin: bool,
    sender_display_name: Optional[str],
    logged_session_id: Optional[int],
    history: List[dict],
    latest_user_content: str,
    emitted_fast_ack_already: Optional[str],
    settings,
):
    """Yield SSE events for the heavy 26B agent path.

    Owns its own DB session for all the setup (RAG, registry,
    capabilities, optional planner, system prompt, AgentTask row)
    because the request-scoped session is closed by the time the
    StreamingResponse starts streaming.

    Mirrors the public SSE contract the UI consumes:

    * ``{"task_id": ...}`` — emitted after AgentTask creation
    * ``{"type": "fast_ack", "text": ...}`` — optional e2b upgrade
      ack that replaces the heuristic one already shown
    * ``{"step": ...}`` / ``{"delta": ...}`` — agent loop events
    * ``{"error": ..., "kind": ...}`` — terminal failure surface
    * ``{"done": true}`` — terminal marker
    """
    setup_db = SessionLocal()
    try:
        rag_block = chat_prompts.build_rag_block(
            setup_db,
            family_id,
            recognized_person_id,
            requestor_is_admin=requestor_is_admin,
        )
        registry = agent_tools.build_default_registry()
        capabilities = agent_tools.detect_capabilities(setup_db, assistant_id)
        capabilities_block = agent_tools.describe_capabilities(
            registry, capabilities
        )

        # Optional dynamic-SQL planner. Disabled by default because a
        # 26B model needs 5-10 s for the extra non-streaming round
        # trip — too slow for conversational use when the static RAG
        # block already covers most household entities.
        live_data_block: Optional[str] = None
        if settings.AI_RAG_PLANNER_ENABLED and latest_user_content:
            try:
                planner_queries = await ai_planner.plan_queries(
                    family_id=family_id,
                    rag_block=rag_block,
                    schema_dump=schema_catalog.dump_text(setup_db),
                    last_user_message=latest_user_content,
                )
            except Exception:  # noqa: BLE001 — planner is best-effort
                logger.exception(
                    "Planner step crashed; continuing without live data"
                )
                planner_queries = []
            if planner_queries:
                logger.info(
                    "[orch] surface=live_chat path=rag_planner+heavy "
                    "family_id=%s n_planner_queries=%d queries=%s",
                    family_id,
                    len(planner_queries),
                    [q[:80] for q in planner_queries],
                )
                live_data_block = (
                    ai_planner.execute_planner_queries(
                        setup_db, family_id, planner_queries
                    )
                    or None
                )

        system = chat_prompts.build_system_prompt(
            setup_db,
            assistant_name=assistant_name,
            family_name=family_name,
            rag_block=rag_block,
            capabilities_block=capabilities_block,
            live_data_block=live_data_block,
            speaker_person_id=recognized_person_id,
            family_id=family_id,
            requestor_is_admin=requestor_is_admin,
        )
        system = (
            system
            + "\n\n--- Tool-use rules ---\n"
            "Use the tools listed under 'What you can do' when the user "
            "asks you to take an action or fetch live data. ALWAYS call "
            "lookup_person to resolve a household member's email before "
            "drafting an email to them. After a tool returns, briefly "
            "confirm what you did (one sentence). Never claim to have "
            "sent an email unless gmail_send returned ok=true."
        )

        task_row = agent_loop.create_task(
            setup_db,
            family_id=family_id,
            live_session_id=logged_session_id,
            person_id=recognized_person_id,
            kind="chat",
            input_text=latest_user_content,
            model=llm._model(),
        )
        setup_db.commit()
        task_id = task_row.agent_task_id
    finally:
        setup_db.close()

    logger.info(
        "[orch] surface=live_chat path=heavy_agent family_id=%s "
        "session=%s task=%s person_id=%s history_msgs=%d "
        "rag_planner=%s instant_acked=%s",
        family_id,
        logged_session_id,
        task_id,
        recognized_person_id,
        len(history),
        bool(live_data_block),
        bool(emitted_fast_ack_already),
    )

    yield f"data: {json.dumps({'task_id': task_id})}\n\n"

    accumulated_text = ""
    terminal_error: Optional[str] = None
    step_summaries: list[dict] = []
    emitted_fast_ack: Optional[str] = emitted_fast_ack_already

    first_delta_seen = asyncio.Event()
    agent_finished = asyncio.Event()
    queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for event in agent_loop.run_agent(
                task_id=task_id,
                family_id=family_id,
                assistant_id=assistant_id,
                person_id=recognized_person_id,
                system_prompt=system,
                history=history,
                user_message=latest_user_content,
                registry=registry,
                capabilities=capabilities,
                requestor_is_admin=requestor_is_admin,
            ):
                await queue.put(("event", event))
        except Exception as exc:  # noqa: BLE001 - never crash the stream
            logger.exception("Agent stream crashed")
            await queue.put(("crash", exc))
        finally:
            agent_finished.set()
            await queue.put(("done", None))

    async def _e2b_upgrade_ack() -> None:
        """Optional second ack from the e2b contextual model.

        The heuristic ack already shipped at byte one of the parent
        stream, so this is pure polish — if e2b returns before the
        heavy agent's first delta we push a nicer wording
        (*"Looking up your calendar..."*) that the UI swaps in for
        the placeholder.
        """
        try:
            ack_text = await fast_ack.generate_contextual_ack_async(
                surface="chat",
                sender_display_name=sender_display_name,
                last_user_message=latest_user_content,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Live chat e2b upgrade ack crashed for task=%s", task_id
            )
            return
        if not ack_text:
            return
        if first_delta_seen.is_set() or agent_finished.is_set():
            return
        await queue.put(("fast_ack", ack_text))

    producer_task = asyncio.create_task(_producer())
    upgrade_task: Optional[asyncio.Task] = None
    if settings.AI_FAST_ACK_ENABLED and latest_user_content:
        upgrade_task = asyncio.create_task(_e2b_upgrade_ack())

    try:
        while True:
            kind, item = await queue.get()
            if kind == "done":
                break
            if kind == "fast_ack":
                emitted_fast_ack = item
                yield (
                    "data: "
                    + json.dumps({"type": "fast_ack", "text": item})
                    + "\n\n"
                )
                logger.info(
                    "Live chat: e2b upgrade ack emitted for task=%s text=%r",
                    task_id,
                    item,
                )
                continue
            if kind == "crash":
                terminal_error = str(item)
                yield (
                    "data: "
                    + json.dumps(
                        {"error": terminal_error, "kind": "error"}
                    )
                    + "\n\n"
                )
                continue
            event = item
            if event.type == "step":
                step = event.payload.get("step") or {}
                step_summaries.append(
                    {
                        "step_index": step.get("step_index"),
                        "step_type": step.get("step_type"),
                        "tool_name": step.get("tool_name"),
                        "duration_ms": step.get("duration_ms"),
                        "error": step.get("error"),
                    }
                )
            if event.type == "delta":
                accumulated_text += event.payload.get("delta", "")
                first_delta_seen.set()
            if event.type == "task_failed":
                terminal_error = (
                    event.payload.get("error") or "unknown error"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {"error": terminal_error, "kind": "agent"}
                    )
                    + "\n\n"
                )
            yield event.to_sse()
    finally:
        if upgrade_task is not None and not upgrade_task.done():
            upgrade_task.cancel()
            try:
                await upgrade_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    if logged_session_id is not None and (
        accumulated_text or terminal_error or emitted_fast_ack
    ):
        log_db = SessionLocal()
        try:
            log_session = log_db.get(models.LiveSession, logged_session_id)
            if log_session is not None:
                if emitted_fast_ack:
                    live_session.log_message(
                        log_db,
                        log_session,
                        role="assistant",
                        content=emitted_fast_ack,
                        person_id=recognized_person_id,
                        meta={
                            "kind": "live_fast_ack",
                            "agent_task_id": task_id,
                            "used_model": llm.fast_model(),
                        },
                    )
                live_session.log_message(
                    log_db,
                    log_session,
                    role="assistant",
                    content=(accumulated_text or "(no response)").strip(),
                    meta={
                        "kind": "chat",
                        "used_model": llm._model(),
                        "agent_task_id": task_id,
                        "agent_steps": step_summaries,
                        "error": terminal_error,
                        "had_fast_ack": bool(emitted_fast_ack),
                    },
                )
                log_db.commit()
        except Exception:  # noqa: BLE001 — logging must never break the stream
            logger.exception("Failed to log assistant chat message")
            log_db.rollback()
        finally:
            log_db.close()

    yield f"data: {json.dumps({'done': True})}\n\n"


def _log_shortcut_transcript(
    *,
    live_session_id: int,
    family_id: int,
    recognized_person_id: Optional[int],
    fast_ack_text: Optional[str],
    final_text: str,
    shortcut_name: str,
    used_model: str,
) -> None:
    """Persist the shortcut path's assistant rows in a fresh DB session.

    The request-scoped session is closed by the time the SSE stream
    starts yielding, so the shortcut branch (which lives inside the
    stream generator) needs its own short-lived session for the
    transcript writes. Logs the optional fast-ack row first so the
    History view replays the same Avi-said-X / Avi-said-Y rhythm the
    user saw.
    """
    log_db = SessionLocal()
    try:
        log_session = log_db.get(models.LiveSession, live_session_id)
        if log_session is None or log_session.family_id != family_id:
            return
        if fast_ack_text:
            live_session.log_message(
                log_db,
                log_session,
                role="assistant",
                content=fast_ack_text,
                person_id=recognized_person_id,
                meta={
                    "kind": "live_fast_ack",
                    "used_model": llm.fast_model(),
                },
            )
        live_session.log_message(
            log_db,
            log_session,
            role="assistant",
            content=final_text,
            person_id=recognized_person_id,
            meta={
                "kind": "chat",
                "shortcut": shortcut_name,
                "used_model": used_model,
                "had_fast_ack": bool(fast_ack_text),
            },
        )
        log_db.commit()
    except Exception:  # noqa: BLE001 — logging must never break the stream
        logger.exception(
            "Shortcut %s: failed to log assistant chat message for "
            "live_session=%s",
            shortcut_name,
            live_session_id,
        )
        log_db.rollback()
    finally:
        log_db.close()
