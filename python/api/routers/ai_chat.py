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

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..ai import ollama
from ..ai import rag
from ..ai import session as live_session
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


def _build_rag_block(
    db: Session, family_id: int, person_id: Optional[int]
) -> str:
    family = db.get(models.Family, family_id)
    if family is None:
        return ""
    parts: List[str] = [rag.build_family_overview(db, family)]
    if person_id is not None:
        person = db.get(models.Person, person_id)
        if person is not None and person.family_id == family_id:
            parts.append("Currently talking to:\n" + rag.build_person_context(db, person))
    return "\n\n".join(parts).strip()


# ---------- Status --------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    info = await ollama.health()
    return StatusResponse(**info)  # type: ignore[arg-type]


# ---------- Greet ---------------------------------------------------------


def _display_name(person: models.Person) -> str:
    # For the out-loud greeting we want Avi to use the person's main
    # name (first_name) rather than a household nickname. Falls back to
    # preferred_name only if first_name is missing.
    return person.first_name or person.preferred_name or f"Person {person.person_id}"


@router.post("/greet", response_model=GreetResponse)
async def greet(payload: GreetRequest, db: Session = Depends(get_db)) -> GreetResponse:
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
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    context = rag.build_person_context(db, person)

    # Fast path — no session tracking, pre-session-feature behaviour.
    if payload.live_session_id is None:
        return GreetResponse(
            family_id=payload.family_id,
            person_id=payload.person_id,
            greeting=f"Hi {_display_name(person)}!",
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

    greeting = f"Hi {_display_name(person)}!"
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
    payload: FollowupRequest, db: Session = Depends(get_db)
) -> FollowupResponse:
    """LLM-generated, one-sentence follow-up aimed at the person's most
    salient goal (or a generic "how are you?" when they haven't set one).
    Runs asynchronously while the instant greeting is already playing.
    """
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    context = rag.build_person_context(db, person)
    goal = rag.pick_goal_for_question(person)

    if goal is not None:
        task = (
            "Ask ONE short, specific, warmly-phrased follow-up question about "
            f"their goal: \"{goal.goal_name}\""
            + (f" — {goal.description}" if goal.description else "")
            + ". Make it conversational, not templated. Single sentence only."
        )
    else:
        task = (
            "Ask ONE short, open-ended question about how they're doing today. "
            "Single sentence only."
        )

    prompt = (
        "You just greeted this family member and want to keep the "
        "conversation going.\n\n"
        f"--- Who you are talking to ---\n{context}\n\n"
        f"--- Your task ---\n{task}\n\n"
        "Reply with only the spoken question — no preamble, no quotes, "
        "no restating their name."
    )

    system = ollama.system_prompt_for_avi(assistant_name, family_name)

    try:
        question = await ollama.generate(
            prompt, system=system, temperature=0.8, max_tokens=120
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

    final_question = (question or "How's your day going?").strip()

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
                    "used_model": ollama._model(),
                },
            )

    return FollowupResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        question=final_question,
        goal_name=goal.goal_name if goal else None,
        used_model=ollama._model(),
    )


# ---------- Chat stream ---------------------------------------------------


@router.post("/chat")
async def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    """Streaming chat endpoint.

    When a ``live_session_id`` is provided the server also records the
    conversation:

    * the *latest* user message is logged before streaming starts — we
      never log the whole history the client resent because that would
      duplicate earlier turns,
    * the full assistant reply is accumulated from the stream and
      written to the transcript when the stream finishes (or errors).

    Because the streaming generator outlives the request-scoped ``db``
    dependency, we open a fresh :class:`SessionLocal` inside the
    generator for the final assistant-message write.
    """
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    rag_block = _build_rag_block(db, payload.family_id, payload.recognized_person_id)
    system = ollama.system_prompt_for_avi(assistant_name, family_name)
    if rag_block:
        system += "\n\n--- Known household context ---\n" + rag_block

    messages = [m.model_dump() for m in payload.messages]

    # Up-front session bookkeeping: verify the session exists and log
    # the latest user message, so the transcript is correct even if the
    # LLM stream fails partway through.
    logged_session_id: Optional[int] = None
    if payload.live_session_id is not None:
        session = db.get(models.LiveSession, payload.live_session_id)
        if session is not None and session.family_id == payload.family_id:
            logged_session_id = session.live_session_id
            latest_user = next(
                (m for m in reversed(payload.messages) if m.role == "user"),
                None,
            )
            if latest_user is not None:
                live_session.log_message(
                    db,
                    session,
                    role="user",
                    content=latest_user.content,
                    person_id=payload.recognized_person_id,
                    meta={"kind": "chat"},
                )
                db.commit()

    async def event_stream():
        # SSE framing — each event is `data: {json}\n\n`. The frontend
        # parses "delta" (token text) and "done" markers.
        accumulated: list[str] = []
        stream_error: Optional[str] = None
        try:
            async for chunk in ollama.chat_stream(messages, system=system):
                accumulated.append(chunk)
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
        except ollama.OllamaUnavailable as e:
            stream_error = f"unavailable: {e}"
            yield f"data: {json.dumps({'error': str(e), 'kind': 'unavailable'})}\n\n"
        except ollama.OllamaError as e:
            stream_error = f"error: {e}"
            yield f"data: {json.dumps({'error': str(e), 'kind': 'error'})}\n\n"

        # Log the assistant reply in its own DB session — the request-
        # scoped one was closed the moment the StreamingResponse started.
        if logged_session_id is not None:
            final_text = "".join(accumulated).strip()
            if final_text or stream_error:
                log_db = SessionLocal()
                try:
                    log_session = log_db.get(models.LiveSession, logged_session_id)
                    if log_session is not None:
                        live_session.log_message(
                            log_db,
                            log_session,
                            role="assistant",
                            content=final_text or "(no response)",
                            meta={
                                "kind": "chat",
                                "used_model": ollama._model(),
                                "error": stream_error,
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
