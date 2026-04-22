"""Shared system-prompt scaffolding for every Avi inbound surface.

Email, SMS, WhatsApp, and Telegram all hand the agent the same kind
of system prompt: who Avi is, what tools are available, the household
context, the speaker's privacy scope, the relevant family RAG, the
person being talked to, and the live database schema. Only the
*verb* describing the surface ("emailing with…" / "chatting with on
Telegram…") and the *trailing 'How to reply' block* differ.

Each inbox previously assembled all that boilerplate inline,
duplicating ~60 lines per surface. This module centralises the
scaffolding so the four inbox files only have to express what's
actually surface-specific (the verb and the reply guidelines).

Live web chat does NOT use this helper today — its prompt is built
in :mod:`api.routers.ai_chat` and includes streaming-only blocks
(face-recognition status, recent transcript) that don't apply to
asynchronous surfaces. Worth revisiting if/when the live-chat
prompt grows another consumer.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..ai import authz, ollama, prompts, rag, schema_catalog
from ..ai import tools as agent_tools
from ..ai.assistants import assistant_id_for_family


def build_inbound_system_prompt(
    db: Session,
    *,
    family_id: int,
    person: models.Person,
    surface_verb: str,
    how_to_reply: str,
    assistant_id: Optional[int] = None,
) -> str:
    """Assemble the full system prompt for an asynchronous inbound surface.

    Parameters
    ----------
    db
        Open database session for RAG / capability queries.
    family_id
        The household the inbound belongs to.
    person
        The :class:`models.Person` row for the speaker. Used for
        relationship-aware RAG redaction and the speaker-scope block.
    surface_verb
        Short verb-phrase describing the surface, e.g. ``"emailing
        with"``, ``"texting with"``, ``"chatting with on Telegram"``.
        Embedded into the "Currently <verb>:" person block.
    how_to_reply
        The trailing "How to reply…" block that's specific to this
        surface (subject/sign-off rules, char limits, formatting
        constraints). Already includes its own ``--- … ---`` heading.
    assistant_id
        The assistant row id to use for capability detection. When
        ``None`` we look it up from ``family_id`` so simple callers
        don't have to plumb it through.
    """
    family = db.get(models.Family, family_id)
    assistant_name = (
        family.assistant.assistant_name if family and family.assistant else "Avi"
    )
    family_name = family.family_name if family else None

    rag_block = ""
    if family is not None:
        rag_block = rag.build_family_overview(
            db, family, requestor_person_id=person.person_id
        )
    person_block = (
        f"Currently {surface_verb}:\n"
        + rag.build_person_context(
            db, person, requestor_person_id=person.person_id
        )
    )

    if assistant_id is None:
        assistant_id = assistant_id_for_family(db, family_id)
    registry = agent_tools.build_default_registry()
    capabilities = agent_tools.detect_capabilities(db, assistant_id)
    capabilities_block = agent_tools.describe_capabilities(registry, capabilities)

    parts: List[str] = [
        ollama.system_prompt_for_avi(assistant_name, family_name),
        "--- What you can do ---\n" + capabilities_block,
    ]
    house_context = prompts.render_context_blocks()
    if house_context:
        parts.append("--- House context ---\n" + house_context)
    parts.append(
        authz.render_speaker_scope_block(
            authz.build_speaker_scope(db, speaker_person_id=person.person_id)
        )
    )
    if rag_block:
        parts.append("--- Known household context ---\n" + rag_block)
    parts.append(person_block)
    parts.append(
        "--- Database schema you can query ---\n"
        "You have read-only access to the family Postgres database. "
        "Sensitive columns are encrypted; use the *_last_four helpers.\n\n"
        + schema_catalog.dump_text(db)
    )
    parts.append(how_to_reply)
    return prompts.with_safety("\n\n".join(parts))


__all__ = ["build_inbound_system_prompt"]
