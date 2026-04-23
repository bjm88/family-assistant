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
from ..ai import ollama
from ..ai import planner as ai_planner
from ..ai import prompts
from ..ai import rag
from ..ai import schema_catalog
from ..ai import session as live_session
from ..ai import sql_tool
from ..ai import tools as agent_tools
from ..ai import web_search_shortcut
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


def _shortcut_stream_response(
    *,
    final_text: str,
    live_session_id: Optional[int],
    family_id: int,
    recognized_person_id: Optional[int],
    user_message: str,
) -> StreamingResponse:
    """Wrap a one-shot web-search-shortcut answer as a SSE stream.

    Matches the shape the live-chat UI already consumes from the
    heavy-agent path so the front-end needs no changes:

    1. ``{"task_id": null}`` — no agent_task row exists for shortcut
       turns (per the "minimal audit" design choice). The UI tolerates
       a null id; the navigation breadcrumb just won't be clickable.
    2. ``{"delta": "..."}`` — the answer text in one chunk.
    3. ``{"type": "task_completed", "summary": ...}`` — synthesised so
       any client logic that waits on ``task_completed`` keeps working.
    4. ``{"done": true}`` — terminal marker, identical to the heavy
       path.

    The user message has already been logged to the live-session
    transcript by the caller (or rather, would have been — but the
    shortcut path runs BEFORE the normal logging step, so we do
    BOTH the user log and the assistant log here in one pass).
    """

    async def _stream():
        yield f"data: {json.dumps({'task_id': None})}\n\n"
        yield f"data: {json.dumps({'delta': final_text})}\n\n"
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "task_completed",
                    "task_id": None,
                    "summary": final_text,
                    "shortcut": "web_search",
                }
            )
            + "\n\n"
        )

        # Persist BOTH messages so the History view shows the full
        # exchange. We open a private session because the request-
        # scoped one was closed when the StreamingResponse started.
        if live_session_id is not None:
            log_db = SessionLocal()
            try:
                log_session = log_db.get(models.LiveSession, live_session_id)
                if log_session is not None and log_session.family_id == family_id:
                    if user_message:
                        live_session.log_message(
                            log_db,
                            log_session,
                            role="user",
                            content=user_message,
                            person_id=recognized_person_id,
                            meta={"kind": "chat"},
                        )
                    live_session.log_message(
                        log_db,
                        log_session,
                        role="assistant",
                        content=final_text,
                        person_id=recognized_person_id,
                        meta={
                            "kind": "chat",
                            "shortcut": "web_search",
                            "used_model": "gemini_grounded",
                        },
                    )
                    log_db.commit()
            except Exception:  # noqa: BLE001 — logging must never break the stream
                logger.exception(
                    "web_search shortcut: failed to log assistant chat "
                    "message for live_session=%s",
                    live_session_id,
                )
                log_db.rollback()
            finally:
                log_db.close()

        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ---------- Status --------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
async def status(_: CurrentUser = Depends(require_user)) -> StatusResponse:
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
    heavy_tag = ollama._model()
    fast_tag = ollama.fast_model()
    heavy_loaded, fast_loaded = await asyncio.gather(
        ollama.warmup_model(heavy_tag),
        ollama.warmup_model(fast_tag),
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
        question, followup_model = await ollama.generate_with_fallback(
            prompt,
            primary_model=ollama.fast_model(),
            system=system,
            temperature=0.8,
            max_tokens=120,
        )
    except ollama.OllamaUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Local LLM is not available: {e}. Start Ollama and run "
                f"`ollama pull {ollama._model()}`."
            ),
        )
    except ollama.OllamaError as e:
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
    """Streaming chat endpoint, driven by the AI agent loop.

    Each invocation creates one ``agent_tasks`` row + N ``agent_steps``
    rows so every tool call Avi makes is auditable. The SSE stream
    emits a mix of these event shapes:

    * ``{"task_id": ...}`` — sent first so the UI can subscribe to the
      task page or render an in-flight progress card.
    * ``{"type": "fast_ack", "text": ...}`` — *optional, at most one
      per turn.* Sent if the heavy agent hasn't started streaming text
      within :setting:`AI_FAST_ACK_AFTER_SECONDS`. The UI shows it as
      a transient placeholder bubble that's replaced when the real
      reply arrives. Mirrors the Telegram / SMS fast-ack pattern so
      every surface behaves the same way.
    * ``{"step": {...}}`` — one event per model thought, tool call, and
      tool result. The UI renders these as inline timeline entries.
    * ``{"delta": "..."}`` — final natural-language reply (sent in one
      chunk after the agent loop converges; older clients that just
      append deltas continue to work unmodified).
    * ``{"done": true}`` — terminal marker; the UI clears its
      "streaming" indicator on this.

    When a ``live_session_id`` is provided the server still records the
    user message (before the agent runs) and the final assistant reply
    (after) into the live-session transcript, so the History page is
    unchanged. The fast-ack, when emitted, is also logged with
    ``meta.kind="live_fast_ack"`` so the History view can replay it
    in order.
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

    # ---------- Fast-path web-search shortcut ---------------------------
    #
    # Before we do ANY heavy setup (RAG, registry, schema dump, system
    # prompt), give the lightweight Gemma classifier ~300 ms to decide
    # whether this turn is a pure web-lookup ask. If yes, we stream
    # Gemini's grounded answer straight back and skip the heavy agent
    # loop entirely (~5-10 s saved). If no — or if anything fails —
    # we fall through to the existing flow with no behavioural change.
    # The classifier itself is bounded by
    # `AI_WEB_SEARCH_SHORTCUT_CLASSIFIER_TIMEOUT_S` and returns
    # `False` on every error path, so this is always safe to call.
    latest_user_for_shortcut = next(
        (m.content for m in reversed(payload.messages) if m.role == "user"),
        "",
    ).strip()
    if (
        latest_user_for_shortcut
        and get_settings().AI_WEB_SEARCH_SHORTCUT_ENABLED
    ):
        # ``try_shortcut`` is total — every failure mode returns
        # ``None`` so we just check the return.
        shortcut_text = await web_search_shortcut.try_shortcut(
            latest_user_for_shortcut
        )
        if shortcut_text:
            logger.info(
                "[orch] surface=live_chat path=web_shortcut "
                "family_id=%s session=%s reply_chars=%d msg=%r "
                "(skipping heavy agent)",
                payload.family_id,
                payload.live_session_id,
                len(shortcut_text),
                latest_user_for_shortcut[:80],
            )
            return _shortcut_stream_response(
                final_text=shortcut_text,
                live_session_id=payload.live_session_id,
                family_id=payload.family_id,
                recognized_person_id=payload.recognized_person_id,
                user_message=latest_user_for_shortcut,
            )

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    assistant_id = _assistant_id_for(db, payload.family_id)
    rag_block = chat_prompts.build_rag_block(
        db, payload.family_id, payload.recognized_person_id
    )

    # Build the tool registry up-front so the same instance is used to
    # (a) describe Avi's capabilities to the model in the system prompt
    # and (b) dispatch real tool calls in the agent loop. Capabilities
    # are inspected here so a tool whose backing integration is offline
    # (no Google account connected, etc.) is hidden from the model
    # entirely — it can't claim to do something it can't do this turn.
    registry = agent_tools.build_default_registry()
    capabilities = agent_tools.detect_capabilities(db, assistant_id)
    capabilities_block = agent_tools.describe_capabilities(registry, capabilities)

    # Optional dynamic-SQL planner. Disabled by default because a
    # 26B-parameter local model needs 5-10 s for the extra non-
    # streaming round trip — too slow for conversational use when the
    # static RAG block already dumps every household entity into the
    # system prompt. Flip AI_RAG_PLANNER_ENABLED=true in .env if you're
    # experimenting with tool-use prompts.
    live_data_block: Optional[str] = None
    if get_settings().AI_RAG_PLANNER_ENABLED:
        latest_user = next(
            (m.content for m in reversed(payload.messages) if m.role == "user"),
            "",
        )
        if latest_user:
            try:
                planner_queries = await ai_planner.plan_queries(
                    family_id=payload.family_id,
                    rag_block=rag_block,
                    schema_dump=schema_catalog.dump_text(db),
                    last_user_message=latest_user,
                )
            except Exception:  # noqa: BLE001 — planner is best-effort
                logger.exception(
                    "Planner step crashed; continuing without live data"
                )
                planner_queries = []
            if planner_queries:
                logger.info(
                    "[orch] surface=live_chat path=rag_planner+heavy_agent "
                    "family_id=%s session=%s n_planner_queries=%d queries=%s",
                    payload.family_id,
                    payload.live_session_id,
                    len(planner_queries),
                    [q[:80] for q in planner_queries],
                )
                live_data_block = (
                    ai_planner.execute_planner_queries(
                        db, payload.family_id, planner_queries
                    )
                    or None
                )

    system = chat_prompts.build_system_prompt(
        db,
        assistant_name=assistant_name,
        family_name=family_name,
        rag_block=rag_block,
        capabilities_block=capabilities_block,
        live_data_block=live_data_block,
        speaker_person_id=payload.recognized_person_id,
    )

    # Tool-use protocol reminder. Goes after the safety + capabilities
    # block; safe to keep here because anything appended after
    # ``with_safety`` is still bracketed by the trailing reinforcer
    # that ``with_safety`` adds.
    system = (
        system
        + "\n\n--- Tool-use rules ---\n"
        "Use the tools listed under 'What you can do' when the user "
        "asks you to take an action or fetch live data. ALWAYS call "
        "lookup_person to resolve a household member's email before "
        "drafting an email to them. After a tool returns, briefly "
        "confirm what you did (one sentence). Never claim to have sent "
        "an email unless gmail_send returned ok=true."
    )

    history = [m.model_dump() for m in payload.messages]
    latest_user_content = next(
        (m["content"] for m in reversed(history) if m["role"] == "user"),
        "",
    )

    # Up-front session bookkeeping: verify the session exists and log
    # the latest user message, so the transcript is correct even if the
    # LLM stream fails partway through.
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

    # Create the audit row up-front so the SSE stream can reference its
    # id immediately. We commit here so external clients (e.g. a future
    # /tasks/{id}/events SSE consumer that reconnects) can find the row.
    task_row = agent_loop.create_task(
        db,
        family_id=payload.family_id,
        live_session_id=logged_session_id,
        person_id=payload.recognized_person_id,
        kind="chat",
        input_text=latest_user_content,
        model=ollama._model(),
    )
    db.commit()
    task_id = task_row.agent_task_id
    logger.info(
        "[orch] surface=live_chat path=heavy_agent_with_ack_race "
        "family_id=%s session=%s task=%s person_id=%s history_msgs=%d "
        "rag_planner=%s",
        payload.family_id,
        payload.live_session_id,
        task_id,
        payload.recognized_person_id,
        len(payload.messages),
        bool(live_data_block),
    )

    # Best display-name for the speaker so the fast-ack prompt can
    # personalise (*"Looking up your calendar..."*) when known. None
    # is fine — the fast-ack module falls back to "the user".
    sender_display_name: Optional[str] = None
    if payload.recognized_person_id is not None:
        speaker = db.get(models.Person, payload.recognized_person_id)
        if speaker is not None and speaker.family_id == payload.family_id:
            sender_display_name = (
                speaker.preferred_name or speaker.first_name
            )

    settings = get_settings()

    async def event_stream():
        # SSE framing — each event is `data: {json}\n\n`.
        # First event tells the UI which task this stream belongs to.
        yield f"data: {json.dumps({'task_id': task_id})}\n\n"

        accumulated_text = ""
        terminal_error: Optional[str] = None
        step_summaries: list[dict] = []  # for live-session transcript

        # Fast-ack race state. ``first_delta_seen`` short-circuits the
        # ack path the moment the heavy model starts streaming text —
        # no point announcing work that's already arriving. The ack,
        # when minted, lands here so the post-stream session-log step
        # can persist it alongside the final reply.
        first_delta_seen = asyncio.Event()
        agent_finished = asyncio.Event()
        emitted_fast_ack: Optional[str] = None

        # Pump agent events through a queue so the watchdog can
        # interleave a `fast_ack` event between two heavy-model events
        # without blocking on the next ``__anext__`` call.
        queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()

        async def _producer() -> None:
            try:
                async for event in agent_loop.run_agent(
                    task_id=task_id,
                    family_id=payload.family_id,
                    assistant_id=assistant_id,
                    person_id=payload.recognized_person_id,
                    system_prompt=system,
                    history=history,
                    user_message=latest_user_content,
                    registry=registry,
                    capabilities=capabilities,
                ):
                    await queue.put(("event", event))
            except Exception as exc:  # noqa: BLE001 - never crash the stream
                logger.exception("Agent stream crashed")
                await queue.put(("crash", exc))
            finally:
                agent_finished.set()
                await queue.put(("done", None))

        async def _ack_watchdog() -> None:
            """Live-chat fast-ack: heuristic instant + e2b upgrade.

            Two-stage delivery so the user always sees *something*
            inside the bubble within ~10 ms, even if Ollama is busy
            or the e2b model is cold:

            1.  Emit a keyword-matched heuristic ack
                (``heuristic_ack``) immediately. This is pure Python
                — no model call — and slots into the bubble before
                the heavy agent has even started loading its prompt.
            2.  Fire the contextual e2b call in parallel with the
                heavy agent. If e2b returns *first*, push it as a
                second ``fast_ack`` event; the UI just replaces the
                heuristic placeholder with the better wording. If
                the heavy agent wins, the e2b result is discarded
                silently (the user already moved past the
                placeholder when the real content streamed in).

            This dodges the previous failure mode: the e2b call
            often gets queued behind the in-flight 26b request, so
            by the time the contextual ack came back the heavy
            delta had already arrived and we'd silently drop it.
            With the heuristic stage, the user always gets visible
            text fast — the e2b stage is just polish.

            Telegram and SMS keep the 3 s threshold via
            ``generate_contextual_ack_sync``; nothing here changes
            their behaviour.
            """
            instant = fast_ack.heuristic_ack(latest_user_content)
            if instant and not first_delta_seen.is_set():
                await queue.put(("fast_ack", instant))
                logger.info(
                    "Live chat fast-ack: heuristic emitted for task=%s "
                    "text=%r (e2b upgrade pending)",
                    task_id,
                    instant,
                )
            try:
                ack_text = await fast_ack.generate_contextual_ack_async(
                    surface="chat",
                    sender_display_name=sender_display_name,
                    last_user_message=latest_user_content,
                )
            except Exception:  # noqa: BLE001 - ack must never crash chat
                logger.exception(
                    "Live chat fast-ack call crashed for task=%s", task_id
                )
                return
            if not ack_text:
                logger.info(
                    "Live chat fast-ack: e2b returned no text for task=%s "
                    "(cold timeout, model not pulled, or feature off) — "
                    "leaving the heuristic placeholder in place",
                    task_id,
                )
                return
            if first_delta_seen.is_set() or agent_finished.is_set():
                logger.info(
                    "Live chat fast-ack: e2b text ready for task=%s but "
                    "heavy agent already finished — keeping heuristic "
                    "and skipping upgrade to avoid flicker",
                    task_id,
                )
                return
            await queue.put(("fast_ack", ack_text))

        producer_task = asyncio.create_task(_producer())
        watchdog_task: Optional[asyncio.Task] = None
        if settings.AI_FAST_ACK_ENABLED and latest_user_content:
            watchdog_task = asyncio.create_task(_ack_watchdog())

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
                        "Live chat: fast-ack emitted for task=%s text=%r",
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
                # kind == "event" — an AgentEvent from the loop.
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
                    # Surface the error in the legacy 'error' shape so
                    # the existing UI banner still lights up.
                    yield (
                        "data: "
                        + json.dumps(
                            {"error": terminal_error, "kind": "agent"}
                        )
                        + "\n\n"
                    )
                yield event.to_sse()
        finally:
            # The producer normally completes on its own; we only need
            # to cancel the watchdog if the agent finished before its
            # threshold fired (otherwise it's a harmless no-op task
            # waiting on ``first_delta_seen``).
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        # Log the fast-ack + assistant reply in their own DB session —
        # the request-scoped one was closed the moment the
        # StreamingResponse started. Two rows so the History view can
        # replay the same Avi-said-X / Avi-said-Y rhythm the user saw.
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
                            person_id=payload.recognized_person_id,
                            meta={
                                "kind": "live_fast_ack",
                                "agent_task_id": task_id,
                                "used_model": ollama.fast_model(),
                            },
                        )
                    live_session.log_message(
                        log_db,
                        log_session,
                        role="assistant",
                        content=(accumulated_text or "(no response)").strip(),
                        meta={
                            "kind": "chat",
                            "used_model": ollama._model(),
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")
