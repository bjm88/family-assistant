"""Shared system-prompt scaffolding for every Avi inbound surface.

Email, SMS, WhatsApp, and Telegram all hand the agent the same kind
of system prompt: who Avi is, the household context, the speaker's
privacy scope, the relevant family RAG, and the person being talked
to. Only the *verb* describing the surface ("emailing with…" /
"chatting with on Telegram…") and the *trailing 'How to reply' block*
differ.

Each inbox previously assembled all that boilerplate inline,
duplicating ~60 lines per surface. This module centralises the
scaffolding so the four inbox files only have to express what's
actually surface-specific (the verb and the reply guidelines).

Token-budget history
--------------------
Earlier revisions of this prompt also injected:

* The full :func:`agent_tools.describe_capabilities` English bullet
  list (~13 K chars). Redundant — the tool JSON schema we send on
  every ``/api/chat`` request already carries ``name`` +
  ``description`` for every tool the model can call.
* The full :func:`schema_catalog.dump_text` live Postgres schema
  (~41 K chars). Redundant for 99 % of inbox turns — the heavy agent
  almost always reaches for a named tool (``calendar_*``, ``task_*``,
  ``gmail_send``, ``lookup_person``) rather than ``sql_query``, and
  the RAG block below already denormalises every household asset it
  might otherwise have queried for.

Dropping both cut the system prompt from ~30 K tokens to ~8 K,
roughly tripled the effective tok/s on ``gemma4:26b`` prefill, and
made the KV cache survive between cycles.

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
from ..ai import authz, ollama, prompts, rag
from ..ai.assistants import assistant_id_for_family


# ---------------------------------------------------------------------------
# Compact "which tool should I reach for?" hint
# ---------------------------------------------------------------------------
# The per-tool JSON schema we send on every ``/api/chat`` request
# already describes what each tool does — what the model occasionally
# *loses* (and the old 41 K schema dump was covering for) is a gentle
# reminder that the specialised tools exist. Without this, ``gemma4``
# sometimes tries to ``sql_query`` its way to "add an event to my
# calendar" or to "look up Ben's email", which is slower *and* wrong
# (sql_query is read-only; it can't create events).
#
# Keep this section TIGHT (<1 K chars). Every character here is
# prefill on every inbound turn.

_TOOL_PREFERENCE_HINT = (
    "--- Tool selection ---\n"
    "Prefer the specialised tools over `sql_query` whenever they fit:\n"
    "- Calendar asks -> `calendar_create_event`, `calendar_list_upcoming`, "
    "`calendar_check_availability`, `calendar_find_free_slots`, "
    "`calendar_list_for_person`.\n"
    "- Task / todo asks -> `task_create`, `task_list`, `task_get`, "
    "`task_update`, `task_add_comment`, `task_add_follower`, "
    "`task_set_schedule`.\n"
    "- Email sending -> `gmail_send` (after `lookup_person` for the address).\n"
    "- Finding a household member -> `lookup_person`.\n"
    "- Revealing a sensitive identifier -> `reveal_sensitive_identifier` "
    "or `reveal_secret`.\n"
    "- Open-web research -> `web_search`.\n"
    "Reach for `sql_query` only when you need an ad-hoc SELECT the other "
    "tools don't cover (counting or filtering household rows). Everything "
    "you normally need is already denormalised into the household-context "
    "block below — check it before writing SQL."
)


# ---------------------------------------------------------------------------
# Compact core-schema hint (for the rare sql_query case)
# ---------------------------------------------------------------------------
# A tiny crib sheet of the most-queried tables and their key columns.
# This is hand-authored (~1.3 K chars) and replaces the auto-generated
# 41 K-char schema dump that used to ship on every inbound turn. If the
# model needs column-level detail for a more exotic table it can ask;
# ``sql_query`` validates the query server-side so a wrong column name
# returns a clean error rather than leaking data.
#
# Keep in sync with ``schema_catalog.ALLOWED_TABLES`` when tables are
# added — but don't try to enumerate every column: brevity is the
# whole point of this hint.

_COMPACT_SCHEMA_HINT = (
    "--- Family database (read-only) ---\n"
    "You have SELECT access to the household Postgres database. Filter "
    "every query by `family_id`. Columns ending in `_encrypted` are "
    "blocked; use the matching `*_last_four` helper or the appropriate "
    "reveal tool. Core tables and their primary keys / most-used columns:\n"
    "- `families` (family_id): family_name, timezone, assistant_id.\n"
    "- `people` (person_id): family_id, first_name, last_name, "
    "preferred_name, email_address, {mobile,home,work}_phone_number, "
    "date_of_birth, gender, primary_family_relationship, "
    "interests_and_activities.\n"
    "- `person_relationships` (person_relationship_id): person_id, "
    "other_person_id, relationship_type.\n"
    "- `goals` (goal_id): person_id, goal_name, priority, target_date.\n"
    "- `jobs` (job_id): person_id, employer_name, role_title, "
    "work_email, is_current.\n"
    "- `medical_conditions` (medical_condition_id): person_id, "
    "condition_name, icd10_code, start_date, end_date.\n"
    "- `medications` (medication_id): person_id, brand_name, generic_name, "
    "dosage, schedule, start_date, end_date.\n"
    "- `physicians` (physician_id): person_id, physician_name, specialty, "
    "practice_name, phone_number.\n"
    "- `pets` (pet_id): family_id, pet_name, animal_type, breed, "
    "date_of_birth.\n"
    "- `residences` (residence_id): family_id, label, street_line_1, "
    "city, state_or_region, postal_code, is_primary_residence.\n"
    "- `vehicles` (vehicle_id): family_id, vehicle_type, year, make, "
    "model, color, primary_driver_person_id, residence_id, "
    "license_plate_number_last_four, vehicle_identification_number_last_four, "
    "registration_expiration_date.\n"
    "- `insurance_policies` (insurance_policy_id): family_id, policy_type, "
    "carrier_name, plan_name, policy_number_last_four, premium_amount_usd, "
    "premium_billing_frequency, deductible_amount_usd, expiration_date, "
    "agent_name.\n"
    "- `financial_accounts` (financial_account_id): family_id, "
    "primary_holder_person_id, account_type, institution_name, "
    "account_nickname, account_number_last_four.\n"
    "- `identity_documents` (identity_document_id): person_id, "
    "document_type, issuing_authority, expiration_date, "
    "document_number_last_four.\n"
    "- `tasks` (task_id): family_id, creator_person_id, assignee_person_id, "
    "owner_kind, task_kind, title, description, status, priority, "
    "due_date, completed_at, cron_expression.\n"
    "- `task_comments`, `task_followers`, `task_attachments` — keyed by "
    "task_id, shape matches the `task_*` tools.\n"
    "If you need columns not listed here, write the SELECT and let the "
    "sql_query tool's error messages guide you — every column is "
    "documented server-side."
)


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
        Open database session for RAG queries.
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
        The assistant row id, plumbed through for future per-surface
        behaviour that wants to know which Avi persona is replying.
        Looked up from ``family_id`` when not supplied.
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

    parts: List[str] = [
        ollama.system_prompt_for_avi(assistant_name, family_name),
        _TOOL_PREFERENCE_HINT,
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
    parts.append(_COMPACT_SCHEMA_HINT)
    parts.append(
        "--- Handling attached files ---\n"
        "When a [Attachment N: …] block appears in the user's message "
        "the file has already been saved on the server and (when it's "
        "an image or readable document) summarised inline for you — "
        "treat that summary as your view of the file. If the user is "
        "ALSO asking you to track or remember it (e.g. 'make a task to "
        "review this property, details attached', 'save this receipt to "
        "the warranty task', 'add this to the camp signup'), call "
        "`task_attach_message_attachment` with the matching "
        "`media_index` AFTER you've created or located the task. Pass "
        "`media_index=0` to attach every file from this message in one "
        "shot. The user can SEE the chip on the kanban card, so always "
        "confirm in your reply which file(s) you attached and to which "
        "task."
    )
    parts.append(how_to_reply)
    return prompts.with_safety("\n\n".join(parts))


__all__ = ["build_inbound_system_prompt"]
