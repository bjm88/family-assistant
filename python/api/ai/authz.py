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


def can_see_calendar_details(
    db: Session,
    *,
    requestor_person_id: Optional[int],
    subject_person_id: int,
    requestor_is_admin: bool = False,
    audit_log: bool = True,
) -> AccessDecision:
    """Decide whether ``requestor`` may see ``subject``'s calendar EVENT detail.

    Calendar detail (event title, location, attendees, organizer) is
    treated more strictly than the SSN-style sensitive matrix:

    * ``self``   — always allowed.
    * ``spouse`` — direct spouses are allowed.
    * everyone else (including parents-of-adult-children, siblings,
      grandparents, in-laws, anonymous speakers) — DENIED. They can
      still see whether the subject is busy or free in a window
      (which is what every other family member is permitted to see),
      but the event title and location are private to the subject
      and their spouse.

    The "parent → child" carve-out from
    :func:`can_access_sensitive` is intentionally NOT carried over
    here because adults' work calendars frequently contain
    confidential business detail (interview slots, performance
    reviews, layoffs) that even a parent shouldn't see by default.

    ``requestor_is_admin``
        Operator override — Avi (logged in as the assistant) and any
        ``ADMIN_EMAILS`` operator bypass the relationship gate. This
        is the same posture as the admin REST endpoints, which let
        admins read every household. Audit log line still records
        ``label='admin'`` so the bypass is recoverable.
    """
    if requestor_is_admin:
        decision = AccessDecision(
            True, "admin", requestor_person_id, subject_person_id
        )
        _log(decision, scope="cal_detail", enabled=audit_log)
        return decision
    decision = _decide_calendar(
        db,
        requestor_person_id=requestor_person_id,
        subject_person_id=subject_person_id,
    )
    _log(decision, scope="cal_detail", enabled=audit_log)
    return decision


def can_access_sensitive(
    db: Session,
    *,
    requestor_person_id: Optional[int],
    subject_person_id: int,
    requestor_is_admin: bool = False,
    audit_log: bool = True,
) -> AccessDecision:
    """Decide whether ``requestor`` may see ``subject``'s sensitive data.

    Cross-family access is refused as a belt-and-suspenders check (see
    ``cross_family`` branch below): even if a relationship row somehow
    links two people in different families, this function denies.
    Privacy is per-household.

    ``requestor_is_admin``
        Operator override — when ``True`` the household relationship
        gate is bypassed entirely and access is granted. Used for the
        AI-as-operator path (Avi logged in as the assistant) and the
        ``ADMIN_EMAILS`` operators (e.g. the head of household when
        debugging). Cross-family is still allowed for admins because
        admins legitimately operate across families in this app.
        Audit log records ``label='admin'``.

    ``audit_log``
        When ``True`` (default), a stable INFO line is emitted per call
        so audits can answer "did Avi reveal Ben's SSN to Lori?".
        Bulk callers (e.g. the family-overview RAG builder, which runs
        this once per person to pre-compute the speaker's access set)
        should pass ``False`` to keep the noise out of the log and
        emit a single summary line of their own instead.
    """
    if requestor_is_admin:
        decision = AccessDecision(
            True, "admin", requestor_person_id, subject_person_id
        )
        _log(decision, enabled=audit_log)
        return decision
    decision = _decide_sensitive(
        db,
        requestor_person_id=requestor_person_id,
        subject_person_id=subject_person_id,
    )
    _log(decision, enabled=audit_log)
    return decision


def _decide_calendar(
    db: Session,
    *,
    requestor_person_id: Optional[int],
    subject_person_id: int,
) -> AccessDecision:
    """Pure decision function for calendar-detail access — no logging."""
    if requestor_person_id is None:
        return AccessDecision(False, "anonymous", None, subject_person_id)
    if requestor_person_id == subject_person_id:
        return AccessDecision(
            True, "self", requestor_person_id, subject_person_id
        )
    subject = db.get(models.Person, subject_person_id)
    if subject is None:
        return AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
    requestor = db.get(models.Person, requestor_person_id)
    if requestor is None:
        return AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
    if requestor.family_id != subject.family_id:
        return AccessDecision(
            False, "cross_family", requestor_person_id, subject_person_id
        )
    if _is_spouse(db, requestor_person_id, subject_person_id):
        return AccessDecision(
            True, "spouse", requestor_person_id, subject_person_id
        )
    return AccessDecision(
        False, "unauthorized", requestor_person_id, subject_person_id
    )


def _decide_sensitive(
    db: Session,
    *,
    requestor_person_id: Optional[int],
    subject_person_id: int,
) -> AccessDecision:
    """Pure decision function for sensitive-data access — no logging."""
    if requestor_person_id is None:
        return AccessDecision(False, "anonymous", None, subject_person_id)
    if requestor_person_id == subject_person_id:
        return AccessDecision(
            True, "self", requestor_person_id, subject_person_id
        )
    subject = db.get(models.Person, subject_person_id)
    if subject is None:
        return AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
    requestor = db.get(models.Person, requestor_person_id)
    if requestor is None:
        return AccessDecision(
            False, "unknown_subject", requestor_person_id, subject_person_id
        )
    if requestor.family_id != subject.family_id:
        return AccessDecision(
            False, "cross_family", requestor_person_id, subject_person_id
        )
    if _is_spouse(db, requestor_person_id, subject_person_id):
        return AccessDecision(
            True, "spouse", requestor_person_id, subject_person_id
        )
    if _is_direct_parent(db, requestor_person_id, subject_person_id):
        return AccessDecision(
            True, "parent", requestor_person_id, subject_person_id
        )
    return AccessDecision(
        False, "unauthorized", requestor_person_id, subject_person_id
    )


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

    ``is_admin`` is set when the requester is operating in admin /
    operator mode (Avi logged in as the assistant, an
    ``ADMIN_EMAILS`` operator). The system-prompt block tells the LLM
    "no relationship gate applies — any field on any family member is
    fair game", and the RAG builder treats every person as accessible.
    """

    speaker_person_id: Optional[int]
    speaker_name: Optional[str]
    spouse_names: List[str]
    children_names: List[str]
    can_access_subject_ids: frozenset[int]  # includes self
    is_admin: bool = False


def build_speaker_scope(
    db: Session,
    *,
    speaker_person_id: Optional[int],
    family_id: Optional[int] = None,
    requestor_is_admin: bool = False,
) -> SpeakerScope:
    """Build the speaker's access window.

    When ``requestor_is_admin`` is set, the speaker is treated as an
    operator with full access. ``family_id`` is used (if given) to
    pre-compute ``can_access_subject_ids`` as "every person in the
    family" so the RAG redactor sees no one as off-limits.
    """
    if requestor_is_admin:
        # Operator path — pre-resolve every person_id in the family
        # so the RAG block never redacts anything for the admin.
        accessible_for_admin: set[int] = set()
        if family_id is not None:
            rows = db.execute(
                select(models.Person.person_id).where(
                    models.Person.family_id == family_id
                )
            ).all()
            accessible_for_admin = {int(r[0]) for r in rows}

        # If the admin has ALSO been recognised as a specific person
        # (face-rec on the live page, etc.) we still surface their
        # name in the prompt — useful so the LLM can address them
        # personally — but the gate is wide open regardless.
        speaker_name: Optional[str] = None
        if speaker_person_id is not None:
            speaker = db.get(models.Person, speaker_person_id)
            if speaker is not None:
                speaker_name = (
                    speaker.preferred_name
                    or speaker.first_name
                    or f"Person {speaker.person_id}"
                )
                accessible_for_admin.add(int(speaker_person_id))

        return SpeakerScope(
            speaker_person_id=speaker_person_id,
            speaker_name=speaker_name,
            spouse_names=[],
            children_names=[],
            can_access_subject_ids=frozenset(accessible_for_admin),
            is_admin=True,
        )

    if speaker_person_id is None:
        return SpeakerScope(None, None, [], [], frozenset(), False)

    speaker = db.get(models.Person, speaker_person_id)
    if speaker is None:
        return SpeakerScope(
            speaker_person_id, None, [], [], frozenset(), False
        )

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
        is_admin=False,
    )


def render_speaker_scope_block(scope: SpeakerScope) -> str:
    """Format a :class:`SpeakerScope` for embedding in the system prompt."""
    if scope.is_admin:
        # Operator / admin path: no household privacy gate, the LLM
        # may share any sensitive field present in the RAG block. We
        # still emit the speaker name when known so Avi can address
        # the admin by name. The wording is intentionally emphatic
        # because small local models otherwise default to "share only
        # YOUR own identifier" behaviour they were trained on, even
        # when the RAG block contains every household member's full
        # sensitive data.
        addressed_as = (
            f" You are talking to {scope.speaker_name} (the household admin)."
            if scope.speaker_name
            else ""
        )
        return (
            "--- Speaker identity & privacy scope ---\n"
            "**ADMIN / OPERATOR MODE — NO PRIVACY GATE APPLIES.**"
            f"{addressed_as} The speaker is a household admin / "
            "operator and has UNLIMITED access to every family "
            "member's data. The household relationship-based privacy "
            "gate (self / spouse / direct parent) does NOT apply to "
            "this conversation. Every person's full sensitive data — "
            "SSN / tax IDs, identity document numbers (driver's "
            "license, passport), financial account & routing numbers, "
            "insurance policy numbers, vehicle VIN / plate, medical "
            "conditions, medications, physicians, personal notes — "
            "may be shared verbatim whenever asked. Any value present "
            "in the household-context block below has ALREADY been "
            "authorised for this speaker; quote it directly. If the "
            "value is missing from the context block, call the "
            "appropriate reveal tool (reveal_sensitive_identifier or "
            "reveal_secret) — both honour admin mode and will return "
            "the plaintext. **Do NOT refuse on privacy / "
            "household-rules grounds in this mode under any "
            "circumstance.** Refusing an admin's question with "
            "\"I can't share that\" is a bug. The only refusals "
            "still appropriate are the global safety bans (no "
            "self-harm assistance, no illegal activity, etc.)."
        )

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
        "field, and you must not try to work around it. Conversely: "
        "any sensitive value (full SSN, account number, etc.) that "
        "DOES appear in the household-context block below has "
        "already been authorised for this speaker by the privacy "
        "gate — share it verbatim when asked, you do not need to "
        "call a reveal tool to confirm."
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


def _log(
    decision: AccessDecision,
    *,
    scope: str = "sensitive",
    enabled: bool = True,
) -> None:
    """One stable line per decision so audits are easy to grep.

    ``scope`` distinguishes the sensitive-data check from the
    calendar-detail check so a future audit can answer "did anyone
    other than Ben himself see Ben's work calendar this week?".

    ``enabled``
        When the caller is in a hot loop (e.g. computing the speaker
        scope across every household member) and will emit its own
        summary line, it can pass ``enabled=False`` to demote each
        per-decision line from INFO to DEBUG. The audit signal is
        preserved at DEBUG for anyone who wants to crank logging up.
    """
    verb = "ALLOW" if decision.allowed else "DENY "
    level = logging.INFO if enabled else logging.DEBUG
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "[authz] %s scope=%s requestor=%s subject=%s reason=%s",
        verb,
        scope,
        decision.requestor_person_id,
        decision.subject_person_id,
        decision.label,
    )


def log_scope_summary(
    *,
    scope: str,
    requestor_person_id: Optional[int],
    allowed_subject_ids: Iterable[int],
    denied_subject_ids: Iterable[int],
) -> None:
    """One INFO line summarizing a bulk authz sweep.

    Bulk callers (currently :func:`build_family_overview`) iterate
    ``can_access_sensitive`` across every person in the household to
    build the speaker's access set. Logging each per-call decision at
    INFO produced 14+ lines per chat turn; we now silence those calls
    and emit this single line instead so the audit story is "speaker
    X could see N people in detail and was denied M". Per-call detail
    is still available at DEBUG.
    """
    allowed = sorted({int(x) for x in allowed_subject_ids if x is not None})
    denied = sorted({int(x) for x in denied_subject_ids if x is not None})
    logger.info(
        "[authz] SUMMARY scope=%s requestor=%s allowed=%d denied=%d "
        "allowed_ids=%s denied_ids=%s",
        scope,
        requestor_person_id,
        len(allowed),
        len(denied),
        allowed,
        denied,
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
    "can_see_calendar_details",
    "log_scope_summary",
    "redact_row",
    "redact_rows",
    "render_speaker_scope_block",
]
