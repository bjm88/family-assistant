"""Relationship-based authorization for the AI assistant.

Single source of truth for "may the person currently talking to Avi
see the sensitive data of THIS other family member?".

Rules
-----
The matrix is intentionally tiny so it's easy to audit:

* ``self``    — a person can always see all of their own data.
* ``spouse``  — direct spouses can see each other's data.
* ``parent``  — a direct parent can see a direct child's data.
* anything else (children → parents, siblings, grandparents,
  grandchildren, in-laws, anonymous speaker) → DENY.

Specifically: a grandparent is a "parent of a parent" — *not* a direct
parent of the grandchild — so they fail the parent check. That's
exactly the user-stated requirement ("If a child asked though, or
grandparent, or anyone else it should not allow").

Sensitive scope
---------------
``SENSITIVE_TABLES`` enumerates the tables whose row contents are
fully gated by this check. ``SENSITIVE_COLUMNS_BY_TABLE`` enumerates
the per-column gating for tables that carry both public and sensitive
fields side-by-side (e.g. ``vehicles`` exposes make/model freely but
gates the plate / VIN last-four because they identify the family in
official records).

Speaker identity
----------------
Avi knows who is talking via :attr:`ToolContext.person_id`, which is
populated by:

* face recognition on the live page (``recognized_person_id``),
* email-inbox poller from ``Person.email_address`` matching,
* (future) phone-number lookup from inbound SMS.

When the speaker is unknown (no face, no email match) the requestor is
treated as anonymous and the gate denies every sensitive lookup.

Audit
-----
Every decision is logged at INFO with a stable, parseable shape so
``rg "[authz]"`` can answer "did Avi read Ben's SSN this week, and
who asked?".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What counts as "sensitive"
# ---------------------------------------------------------------------------


# Whole-row-sensitive tables. If the requestor cannot access the row's
# subject person, every column except the FK linkage is redacted. The
# SQL tool sanitiser uses this list directly.
SENSITIVE_TABLES: frozenset[str] = frozenset(
    {
        "sensitive_identifiers",
        "identity_documents",
        "financial_accounts",
        "medical_conditions",
        "medications",
        "physicians",
    }
)


# Per-column sensitive subsets for tables that carry both public and
# private fields. Currently just ``people`` (notes is private; name /
# age / interests are not).
SENSITIVE_COLUMNS_BY_TABLE: dict[str, frozenset[str]] = {
    "people": frozenset({"notes"}),
}


# Per-row "owner" column. The SQL sanitiser uses this to find the
# subject person_id of each returned row so it can decide whether to
# redact.
OWNER_COLUMN_BY_TABLE: dict[str, str] = {
    "sensitive_identifiers": "person_id",
    "identity_documents": "person_id",
    "financial_accounts": "primary_holder_person_id",
    "medical_conditions": "person_id",
    "medications": "person_id",
    "physicians": "person_id",
    "people": "person_id",
}


REDACTED_PLACEHOLDER = "[REDACTED — relationship-based privacy]"


# ---------------------------------------------------------------------------
# Decision API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessDecision:
    """Outcome of an authz check."""

    allowed: bool
    # Stable string label for logs / audit / system prompt:
    # 'self', 'spouse', 'parent', 'unauthorized', 'anonymous',
    # 'cross_family', 'unknown_subject'.
    label: str
    requestor_person_id: Optional[int]
    subject_person_id: int

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


def can_access_sensitive(
    db: Session,
    *,
    requestor_person_id: Optional[int],
    subject_person_id: int,
    family_id: Optional[int] = None,
) -> AccessDecision:
    """Decide whether ``requestor`` may see ``subject``'s sensitive data.

    ``family_id`` is optional but recommended — when supplied we also
    enforce that subject and requestor belong to the same family so a
    bug elsewhere can't accidentally leak between households.
    """
    # Anonymous speaker — face/email lookup didn't identify anyone.
    if requestor_person_id is None:
        decision = AccessDecision(False, "anonymous", None, subject_person_id)
        _log(decision)
        return decision

    if requestor_person_id == subject_person_id:
        decision = AccessDecision(
            True, "self", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    subject = db.get(models.Person, subject_person_id)
    if subject is None:
        decision = AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    requestor = db.get(models.Person, requestor_person_id)
    if requestor is None:
        decision = AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    # Cross-family belt-and-suspenders. Even if a relationship row
    # somehow links them, refuse: privacy is per-household.
    if requestor.family_id != subject.family_id:
        decision = AccessDecision(
            False, "cross_family", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    if _is_spouse(db, requestor_person_id, subject_person_id):
        decision = AccessDecision(
            True, "spouse", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    if _is_direct_parent(db, requestor_person_id, subject_person_id):
        decision = AccessDecision(
            True, "parent", requestor_person_id, subject_person_id
        )
        _log(decision)
        return decision

    decision = AccessDecision(
        False, "unauthorized", requestor_person_id, subject_person_id
    )
    _log(decision)
    return decision


# ---------------------------------------------------------------------------
# Speaker scope summary (for the system prompt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeakerScope:
    """Speaker's entire relationship-based access window for the family.

    Used by the system-prompt builder so the LLM also "knows" which
    family members it may discuss in detail vs. only by name. The
    technical sanitiser still enforces the rules on its own — this is
    purely for response quality.
    """

    speaker_person_id: Optional[int]
    speaker_name: Optional[str]
    spouse_names: List[str]
    children_names: List[str]
    can_access_subject_ids: frozenset[int]  # includes self


def build_speaker_scope(
    db: Session, *, speaker_person_id: Optional[int]
) -> SpeakerScope:
    if speaker_person_id is None:
        return SpeakerScope(None, None, [], [], frozenset())

    speaker = db.get(models.Person, speaker_person_id)
    if speaker is None:
        return SpeakerScope(speaker_person_id, None, [], [], frozenset())

    spouse_ids = _spouse_ids_of(db, speaker_person_id)
    child_ids = _direct_child_ids_of(db, speaker_person_id)
    accessible: set[int] = {speaker_person_id, *spouse_ids, *child_ids}

    spouse_names = _names_for(db, spouse_ids)
    child_names = _names_for(db, child_ids)
    speaker_name = (
        speaker.preferred_name
        or speaker.first_name
        or f"Person {speaker.person_id}"
    )

    return SpeakerScope(
        speaker_person_id=speaker_person_id,
        speaker_name=speaker_name,
        spouse_names=spouse_names,
        children_names=child_names,
        can_access_subject_ids=frozenset(accessible),
    )


def render_speaker_scope_block(scope: SpeakerScope) -> str:
    """Format a :class:`SpeakerScope` for embedding in the system prompt."""
    if scope.speaker_person_id is None or scope.speaker_name is None:
        return (
            "--- Speaker identity & privacy scope ---\n"
            "The person currently talking to you is NOT identified — face "
            "recognition didn't return a match and (if this came via "
            "email) the sender's address didn't match any registered "
            "family member. Treat them as anonymous: do NOT reveal "
            "anyone's SSN, ID document numbers, financial account "
            "numbers, medical conditions, medications, physicians, or "
            "personal notes. Stick to public facts only (names, ages, "
            "interests)."
        )

    spouses = ", ".join(scope.spouse_names) if scope.spouse_names else "none"
    children = (
        ", ".join(scope.children_names) if scope.children_names else "none"
    )
    return (
        "--- Speaker identity & privacy scope ---\n"
        f"You are currently talking to {scope.speaker_name} "
        f"(person_id={scope.speaker_person_id}). They may see the "
        "sensitive data (SSN / tax IDs, identity document numbers, "
        "financial account numbers, medical conditions, medications, "
        "physicians, personal notes) of:\n"
        f"  - themselves\n"
        f"  - their spouse(s): {spouses}\n"
        f"  - their direct children: {children}\n"
        "For ANY other family member (their parents, siblings, "
        "grandparents, grandchildren, in-laws, etc.) you must refer "
        "only to public facts (name, age, gender, role, interests / "
        "hobbies). Refuse politely if asked for sensitive details "
        "about anyone outside the access list above. The backend "
        "ALSO redacts those fields automatically — if you see "
        f"{REDACTED_PLACEHOLDER!r} in tool output, that is the "
        "system telling you the speaker is not allowed to read that "
        "field, and you must not try to work around it."
    )


# ---------------------------------------------------------------------------
# Row / dict redaction helpers (used by lookup_person + sql_tool sanitiser)
# ---------------------------------------------------------------------------


def redact_row(
    row: dict,
    *,
    table_name: str,
    accessible_subject_ids: Iterable[int],
) -> dict:
    """Return a copy of ``row`` with sensitive fields redacted as needed.

    The decision is row-local: we look up the row's "owner" person id
    via :data:`OWNER_COLUMN_BY_TABLE`. If the owner isn't in
    ``accessible_subject_ids`` then either the whole row (for
    fully-sensitive tables) or just the listed columns (for partially-
    sensitive tables) get the redacted placeholder.
    """
    accessible = frozenset(int(x) for x in accessible_subject_ids if x is not None)
    owner_col = OWNER_COLUMN_BY_TABLE.get(table_name)
    owner_id = row.get(owner_col) if owner_col else None

    out = dict(row)
    if owner_id is None or int(owner_id) in accessible:
        return out

    if table_name in SENSITIVE_TABLES:
        # Whole row — keep only the FK column itself so the LLM still
        # knows "this row exists" for an authorized requestor's audit.
        for key in list(out.keys()):
            if key == owner_col:
                continue
            out[key] = REDACTED_PLACEHOLDER
        return out

    cols = SENSITIVE_COLUMNS_BY_TABLE.get(table_name, frozenset())
    for col in cols:
        if col in out:
            out[col] = REDACTED_PLACEHOLDER
    return out


def redact_rows(
    rows: List[dict],
    *,
    table_name: str,
    accessible_subject_ids: Iterable[int],
) -> List[dict]:
    accessible_set = frozenset(
        int(x) for x in accessible_subject_ids if x is not None
    )
    return [
        redact_row(r, table_name=table_name, accessible_subject_ids=accessible_set)
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Internal: graph traversal
# ---------------------------------------------------------------------------


def _is_spouse(db: Session, a: int, b: int) -> bool:
    return db.execute(
        select(models.PersonRelationship.person_relationship_id)
        .where(models.PersonRelationship.relationship_type == "spouse_of")
        .where(
            (
                (models.PersonRelationship.from_person_id == a)
                & (models.PersonRelationship.to_person_id == b)
            )
            | (
                (models.PersonRelationship.from_person_id == b)
                & (models.PersonRelationship.to_person_id == a)
            )
        )
        .limit(1)
    ).scalar_one_or_none() is not None


def _is_direct_parent(db: Session, parent_id: int, child_id: int) -> bool:
    """True iff a direct ``parent_of`` edge from parent_id → child_id exists.

    Grandparents (parent of a parent) are intentionally NOT direct
    parents — that's how grandparents end up denied.
    """
    return db.execute(
        select(models.PersonRelationship.person_relationship_id)
        .where(models.PersonRelationship.relationship_type == "parent_of")
        .where(models.PersonRelationship.from_person_id == parent_id)
        .where(models.PersonRelationship.to_person_id == child_id)
        .limit(1)
    ).scalar_one_or_none() is not None


def _spouse_ids_of(db: Session, person_id: int) -> List[int]:
    rows = db.execute(
        select(
            models.PersonRelationship.from_person_id,
            models.PersonRelationship.to_person_id,
        )
        .where(models.PersonRelationship.relationship_type == "spouse_of")
        .where(
            (models.PersonRelationship.from_person_id == person_id)
            | (models.PersonRelationship.to_person_id == person_id)
        )
    ).all()
    out: set[int] = set()
    for f, t in rows:
        out.add(t if f == person_id else f)
    return sorted(out)


def _direct_child_ids_of(db: Session, parent_id: int) -> List[int]:
    rows = db.execute(
        select(models.PersonRelationship.to_person_id)
        .where(models.PersonRelationship.relationship_type == "parent_of")
        .where(models.PersonRelationship.from_person_id == parent_id)
    ).all()
    return sorted(r[0] for r in rows)


def _names_for(db: Session, ids: Iterable[int]) -> List[str]:
    ids = list(ids)
    if not ids:
        return []
    rows = db.execute(
        select(models.Person).where(models.Person.person_id.in_(ids))
    ).scalars().all()
    return [
        p.preferred_name or p.first_name or f"Person {p.person_id}" for p in rows
    ]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _log(decision: AccessDecision) -> None:
    """One stable line per decision so audits are easy to grep."""
    verb = "ALLOW" if decision.allowed else "DENY "
    logger.info(
        "[authz] %s requestor=%s subject=%s reason=%s",
        verb,
        decision.requestor_person_id,
        decision.subject_person_id,
        decision.label,
    )


__all__ = [
    "AccessDecision",
    "OWNER_COLUMN_BY_TABLE",
    "REDACTED_PLACEHOLDER",
    "SENSITIVE_COLUMNS_BY_TABLE",
    "SENSITIVE_TABLES",
    "SpeakerScope",
    "build_speaker_scope",
    "can_access_sensitive",
    "redact_row",
    "redact_rows",
    "render_speaker_scope_block",
]
