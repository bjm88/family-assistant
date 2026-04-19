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
from ..ai import agent as agent_loop
from ..ai import authz
from ..ai import ollama
from ..ai import prompts
from ..ai import rag
from ..ai import schema_catalog
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


def _assistant_id_for(db: Session, family_id: int) -> Optional[int]:
    """Best-effort lookup of the assistant row (needed for Google tools)."""
    family = db.get(models.Family, family_id)
    if family is None or family.assistant is None:
        return None
    return family.assistant.assistant_id


def _build_rag_block(
    db: Session, family_id: int, person_id: Optional[int]
) -> str:
    """Static context block: family overview + (optional) speaker focus.

    Excludes the schema catalog — that's appended separately by
    :func:`_build_system_prompt` so it can be reused across the planner
    + main chat call without rebuilding.

    ``person_id`` here is the *speaker* (the recognized person Avi is
    talking to). We pass it into the RAG builders as the requestor so
    sensitive details about other family members are pre-redacted in
    the static context — the LLM literally never sees the secrets it
    isn't allowed to share. See :mod:`ai.authz`.
    """
    family = db.get(models.Family, family_id)
    if family is None:
        return ""
    parts: List[str] = [
        rag.build_family_overview(db, family, requestor_person_id=person_id)
    ]
    if person_id is not None:
        person = db.get(models.Person, person_id)
        if person is not None and person.family_id == family_id:
            parts.append(
                "Currently talking to:\n"
                + rag.build_person_context(
                    db, person, requestor_person_id=person_id
                )
            )
    return "\n\n".join(parts).strip()


def _build_system_prompt(
    db: Session,
    *,
    assistant_name: str,
    family_name: Optional[str],
    rag_block: str,
    capabilities_block: str = "",
    live_data_block: Optional[str] = None,
    speaker_person_id: Optional[int] = None,
) -> str:
    """Assemble the full system prompt and wrap it in the safety sandbox.

    Section order (top to bottom inside the safety frame):

    1. Persona — who Avi is.
    2. Capabilities — dynamic list of tools the model can use this
       turn, with example questions. Lets "what can you do?" produce
       accurate answers without hard-coding them in code.
    3. House context — every ``ai_context_*.txt`` at the project root.
    4. Household RAG context — DB-derived family overview.
    5. Database schema + (optional) live SQL results.

    The whole stack is then wrapped by :func:`prompts.with_safety` so
    the unbreakable rules sit OUTSIDE everything below them and can't
    be overridden by anything appended later in the conversation.
    """
    parts: List[str] = [ollama.system_prompt_for_avi(assistant_name, family_name)]
    if capabilities_block:
        parts.append("--- What you can do ---\n" + capabilities_block)
    house_context = prompts.render_context_blocks()
    if house_context:
        parts.append("--- House context ---\n" + house_context)
    # Speaker identity + privacy scope sits BETWEEN the house context
    # and the household RAG so the LLM reads "who is talking and what
    # may they see" before it ingests the (already-redacted) family
    # data block. Without a speaker we still emit the block — it tells
    # the model to treat the conversation as anonymous and clamp down.
    parts.append(
        authz.render_speaker_scope_block(
            authz.build_speaker_scope(db, speaker_person_id=speaker_person_id)
        )
    )
    if rag_block:
        parts.append("--- Known household context ---\n" + rag_block)
    parts.append(
        "--- Database schema you can query ---\n"
        "You have read-only access to the family Postgres database. "
        "When the household context above isn't enough, the orchestrator "
        "may run additional SELECT queries on your behalf and inject the "
        "results below as 'Live data'. Tables and columns are documented "
        "with comments — read them carefully. Sensitive columns (VINs, "
        "account numbers, SSNs) are stored encrypted and are NOT exposed; "
        "use the *_last_four helper columns instead.\n\n"
        + schema_catalog.dump_text(db)
    )
    if live_data_block:
        parts.append("--- Live data (just queried for this turn) ---\n" + live_data_block)
    return prompts.with_safety("\n\n".join(parts))


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

    # If the user is already mid-conversation in this session — e.g.
    # they were typing chat messages for a few minutes and only just
    # now glanced at the camera — a sudden "Hi Ben!" is jarring. Mark
    # them greeted silently and stay quiet. We use the same atomic
    # CAS so concurrent /greet calls still resolve to a single
    # winner; only the caller that flipped False→True needs to do
    # any work, and even that work becomes a no-op return here.
    has_chat_history = (
        db.query(models.LiveSessionMessage)
        .filter(
            models.LiveSessionMessage.live_session_id == session.live_session_id,
            models.LiveSessionMessage.role.in_(("user", "assistant")),
        )
        .filter(
            (models.LiveSessionMessage.meta.is_(None))
            | (models.LiveSessionMessage.meta["kind"].as_string() == "chat")
        )
        .first()
        is not None
    )
    if has_chat_history:
        live_session.mark_greeted(db, participant)
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
def run_sql(payload: SqlRequest, db: Session = Depends(get_db)) -> SqlResponse:
    """Execute a sandboxed read-only SELECT.

    Used by the chat planner under the hood and exposed directly so
    operators can poke at the schema with the same allow-list /
    timeouts the LLM operates under.
    """
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


# ---------- Planner: pick which SELECTs to run before chatting -----------


_PLANNER_INSTRUCTIONS = (
    "You are the data-fetching layer for a family AI assistant. Decide "
    "which database SELECT queries (if any) would help answer the user's "
    "next message. Use the schema documentation in the system prompt.\n\n"
    "Rules:\n"
    "* Reply with ONLY a JSON object: {\"queries\": [\"SELECT ...\", ...]}.\n"
    "* Use [] when the household context already contains the answer.\n"
    "* Each query MUST be a single SELECT or WITH ... SELECT, no semicolons.\n"
    "* Always scope by family_id = {family_id} where the table has it.\n"
    "* Prefer narrow column lists; never SELECT * on people, vehicles, "
    "  insurance_policies, or financial_accounts.\n"
    "* Do not query encrypted columns (anything ending in _encrypted) — "
    "  use the *_last_four helpers.\n"
    "* Cap at 3 queries per turn.\n"
)


async def _plan_queries(
    *,
    family_id: int,
    rag_block: str,
    schema_dump: str,
    last_user_message: str,
) -> List[str]:
    """Ask the LLM which SELECTs (if any) to pre-run for this turn."""
    instructions = _PLANNER_INSTRUCTIONS.replace("{family_id}", str(family_id))
    prompt = (
        f"{instructions}\n\n"
        f"--- Household context ---\n{rag_block}\n\n"
        f"--- Schema ---\n{schema_dump}\n\n"
        f"--- User just said ---\n{last_user_message}\n\n"
        'Reply with a JSON object only, e.g. {"queries": []}.'
    )
    # The planner is a structured-output task — Gemma's tiny e2b
    # variant runs it in well under a second on Apple Silicon, so we
    # never want to burn a 26B-parameter pass on it. The fallback
    # wrapper auto-routes to the main chat model when the fast tag
    # isn't pulled (yet), so a missing model doesn't break chat.
    try:
        raw, _ = await ollama.generate_with_fallback(
            prompt,
            primary_model=ollama.fast_model(),
            system=prompts.with_safety(
                "You return strict JSON. No prose. No markdown fences."
            ),
            temperature=0.1,
            max_tokens=400,
        )
    except ollama.OllamaError as e:
        logger.warning("Planner call failed: %s", e)
        return []

    queries = _parse_planner_output(raw)
    return queries[:3]


def _parse_planner_output(raw: str) -> List[str]:
    """Extract a JSON queries list from the planner reply, tolerantly."""
    if not raw:
        return []
    text_value = raw.strip()
    # Strip ``` fences if the model added them despite instructions.
    if text_value.startswith("```"):
        text_value = text_value.strip("`")
        # drop optional `json` language tag
        if text_value.lower().startswith("json"):
            text_value = text_value[4:]
        text_value = text_value.strip()
    # Locate the first {...} block to be resilient to leading text.
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        obj = json.loads(text_value[start : end + 1])
    except json.JSONDecodeError:
        return []
    qs = obj.get("queries") if isinstance(obj, dict) else None
    if not isinstance(qs, list):
        return []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()]


def _execute_planner_queries(
    db: Session, family_id: int, queries: List[str]
) -> str:
    """Run each planner-generated query, return a prompt-ready block.

    Failures are surfaced inline so the LLM can see what didn't work and
    avoid suggesting the same query the next turn.
    """
    if not queries:
        return ""
    sections: List[str] = []
    for i, q in enumerate(queries, 1):
        header = f"### Query {i}\n```sql\n{q}\n```"
        try:
            result = sql_tool.run_safe_query(db, q, family_id=family_id)
        except sql_tool.SqlToolError as e:
            sections.append(f"{header}\nError: {e}")
            continue
        if not result.rows:
            sections.append(f"{header}\nNo rows.")
            continue
        # Render as a tiny table — small enough that the model can read
        # it, structured enough that it doesn't get parsed as prose.
        body = json.dumps(result.rows, ensure_ascii=False, default=str)
        truncation = " (truncated)" if result.truncated else ""
        sections.append(
            f"{header}\nRows ({result.row_count}{truncation}): {body}"
        )
    return "\n\n".join(sections)


# ---------- Chat stream ---------------------------------------------------


@router.post("/chat")
async def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    """Streaming chat endpoint, driven by the AI agent loop.

    Each invocation creates one ``agent_tasks`` row + N ``agent_steps``
    rows so every tool call Avi makes is auditable. The SSE stream
    emits a mix of three event shapes:

    * ``{"task_id": ...}`` — sent first so the UI can subscribe to the
      task page or render an in-flight progress card.
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
    unchanged.
    """
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")

    assistant_name, family_name = _load_assistant(db, payload.family_id)
    assistant_id = _assistant_id_for(db, payload.family_id)
    rag_block = _build_rag_block(db, payload.family_id, payload.recognized_person_id)

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
                planner_queries = await _plan_queries(
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
                    "RAG planner ran %d query/queries for family_id=%s: %s",
                    len(planner_queries),
                    payload.family_id,
                    [q[:80] for q in planner_queries],
                )
                live_data_block = (
                    _execute_planner_queries(
                        db, payload.family_id, planner_queries
                    )
                    or None
                )

    system = _build_system_prompt(
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

    async def event_stream():
        # SSE framing — each event is `data: {json}\n\n`.
        # First event tells the UI which task this stream belongs to.
        yield f"data: {json.dumps({'task_id': task_id})}\n\n"

        accumulated_text = ""
        terminal_error: Optional[str] = None
        step_summaries: list[dict] = []  # for live-session transcript

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
                if event.type == "task_failed":
                    terminal_error = event.payload.get("error") or "unknown error"
                    # Surface the error in the legacy 'error' shape so
                    # the existing UI banner still lights up.
                    yield (
                        "data: "
                        + json.dumps({"error": terminal_error, "kind": "agent"})
                        + "\n\n"
                    )
                yield event.to_sse()
        except Exception as exc:  # noqa: BLE001 - never crash the stream
            logger.exception("Agent stream crashed")
            terminal_error = str(exc)
            yield f"data: {json.dumps({'error': str(exc), 'kind': 'error'})}\n\n"

        # Log the assistant reply in its own DB session — the request-
        # scoped one was closed the moment the StreamingResponse started.
        if logged_session_id is not None and (accumulated_text or terminal_error):
            log_db = SessionLocal()
            try:
                log_session = log_db.get(models.LiveSession, logged_session_id)
                if log_session is not None:
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
