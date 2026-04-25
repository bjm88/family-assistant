"""Prompt + RAG-block assembly for the live (synchronous) chat endpoint.

Carved out of :mod:`api.routers.ai_chat` so the streaming router can
focus on SSE wiring and audit bookkeeping. The async inbox surfaces
(SMS / Telegram / email / WhatsApp) build their system prompts via
:mod:`api.services.inbound_prompts` instead — that module is shaped
around a "surface verb + how to reply" template, while this one is
shaped around the live-chat needs (capability list, household RAG,
database schema dump, optional planner-fetched live data).
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .. import models
from . import authz, ollama, prompts, rag, schema_catalog


def build_rag_block(
    db: Session,
    family_id: int,
    person_id: Optional[int],
    *,
    requestor_is_admin: bool = False,
) -> str:
    """Static context block: family overview + (optional) speaker focus.

    Excludes the schema catalog — that's appended separately by
    :func:`build_system_prompt` so it can be reused across the planner
    + main chat call without rebuilding.

    ``person_id`` here is the *speaker* (the recognized person Avi is
    talking to). We pass it into the RAG builders as the requestor so
    sensitive details about other family members are pre-redacted in
    the static context — the LLM literally never sees the secrets it
    isn't allowed to share. See :mod:`api.ai.authz`.

    ``requestor_is_admin`` is the operator-bypass flag for Avi (the
    AI logged in as itself) and ``ADMIN_EMAILS`` operators. When set,
    every household member's sensitive data is included in the RAG
    block so the assistant can answer cross-family questions without
    a reveal-tool round-trip.
    """
    family = db.get(models.Family, family_id)
    if family is None:
        return ""
    parts: List[str] = [
        rag.build_family_overview(
            db,
            family,
            requestor_person_id=person_id,
            requestor_is_admin=requestor_is_admin,
        )
    ]
    if person_id is not None:
        person = db.get(models.Person, person_id)
        if person is not None and person.family_id == family_id:
            parts.append(
                "Currently talking to:\n"
                + rag.build_person_context(
                    db,
                    person,
                    requestor_person_id=person_id,
                    requestor_is_admin=requestor_is_admin,
                )
            )
    return "\n\n".join(parts).strip()


def build_system_prompt(
    db: Session,
    *,
    assistant_name: str,
    family_name: Optional[str],
    rag_block: str,
    capabilities_block: str = "",
    live_data_block: Optional[str] = None,
    speaker_person_id: Optional[int] = None,
    family_id: Optional[int] = None,
    requestor_is_admin: bool = False,
) -> str:
    """Assemble the full system prompt and wrap it in the safety sandbox.

    Section order (top to bottom inside the safety frame):

    1. Persona — who Avi is.
    2. Capabilities — dynamic list of tools the model can use this
       turn, with example questions. Lets "what can you do?" produce
       accurate answers without hard-coding them in code.
    3. House context — every ``ai_context_*.txt`` at the project root.
    4. Speaker scope — who is talking and what they're allowed to see.
    5. Household RAG context — DB-derived family overview.
    6. Database schema + (optional) live SQL results.

    The whole stack is then wrapped by :func:`prompts.with_safety` so
    the unbreakable rules sit OUTSIDE everything below them and can't
    be overridden by anything appended later in the conversation.
    """
    parts: List[str] = [
        ollama.system_prompt_for_avi(assistant_name, family_name)
    ]
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
            authz.build_speaker_scope(
                db,
                speaker_person_id=speaker_person_id,
                family_id=family_id,
                requestor_is_admin=requestor_is_admin,
            )
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
        parts.append(
            "--- Live data (just queried for this turn) ---\n"
            + live_data_block
        )
    return prompts.with_safety("\n\n".join(parts))


__all__ = ["build_rag_block", "build_system_prompt"]
