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
from ..db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["ai_chat"])


# ---------- Schemas -------------------------------------------------------


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class GreetRequest(BaseModel):
    family_id: int
    person_id: int
    # Kept for backwards compatibility with any early caller; the greet
    # path is now pure template and never hits the LLM. Use /followup
    # for the goal-based question.
    include_goal_question: bool = False


class GreetResponse(BaseModel):
    family_id: int
    person_id: int
    greeting: str
    # "template" for the instant path, "<model>" when the LLM was used.
    used_model: str
    context_preview: str


class FollowupRequest(BaseModel):
    family_id: int
    person_id: int


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
    """
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    context = rag.build_person_context(db, person)
    return GreetResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        greeting=f"Hi {_display_name(person)}!",
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

    return FollowupResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        question=(question or "How's your day going?").strip(),
        goal_name=goal.goal_name if goal else None,
        used_model=ollama._model(),
    )


# ---------- Chat stream ---------------------------------------------------


@router.post("/chat")
async def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    rag_block = _build_rag_block(db, payload.family_id, payload.recognized_person_id)
    system = ollama.system_prompt_for_avi(assistant_name, family_name)
    if rag_block:
        system += "\n\n--- Known household context ---\n" + rag_block

    messages = [m.model_dump() for m in payload.messages]

    async def event_stream():
        # SSE framing — each event is `data: {json}\n\n`. The frontend
        # parses "delta" (token text) and "done" markers.
        try:
            async for chunk in ollama.chat_stream(messages, system=system):
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
        except ollama.OllamaUnavailable as e:
            yield f"data: {json.dumps({'error': str(e), 'kind': 'unavailable'})}\n\n"
        except ollama.OllamaError as e:
            yield f"data: {json.dumps({'error': str(e), 'kind': 'error'})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
