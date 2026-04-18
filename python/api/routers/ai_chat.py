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
    include_goal_question: bool = True


class GreetResponse(BaseModel):
    family_id: int
    person_id: int
    greeting: str
    used_model: str
    context_preview: str


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


@router.post("/greet", response_model=GreetResponse)
async def greet(payload: GreetRequest, db: Session = Depends(get_db)) -> GreetResponse:
    person = db.get(models.Person, payload.person_id)
    if person is None or person.family_id != payload.family_id:
        raise HTTPException(status_code=404, detail="Person not found in this family")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    context = rag.build_person_context(db, person)
    family = db.get(models.Family, payload.family_id)
    family_overview = rag.build_family_overview(db, family) if family else ""

    goal = rag.pick_goal_for_question(person) if payload.include_goal_question else None

    instructions: List[str] = [
        "Greet them warmly by their preferred name (use exactly one greeting line).",
        "Keep the full response to 2 short sentences, maximum 40 words.",
    ]
    if goal is not None:
        instructions.append(
            "After the greeting, ask ONE genuinely curious, specific follow-up "
            f"question about their goal: \"{goal.goal_name}\""
            + (f" — {goal.description}" if goal.description else "")
            + ". Make the question feel fresh, not templated."
        )
    else:
        instructions.append(
            "After the greeting, ask one short, open-ended question about "
            "how they're doing today."
        )

    prompt = (
        "You just saw the following family member walk into the room.\n\n"
        f"--- Family context ---\n{family_overview}\n\n"
        f"--- Who you are talking to ---\n{context}\n\n"
        f"--- Your task ---\n" + "\n".join(f"- {x}" for x in instructions)
        + "\n\nReply with only the spoken greeting, no preamble, no quotes."
    )

    system = ollama.system_prompt_for_avi(assistant_name, family_name)

    try:
        greeting = await ollama.generate(prompt, system=system, temperature=0.8)
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

    return GreetResponse(
        family_id=payload.family_id,
        person_id=payload.person_id,
        greeting=greeting or f"Hi {person.preferred_name or person.first_name}!",
        used_model=ollama._model(),
        context_preview=context,
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
