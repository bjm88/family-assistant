"""``reveal_sensitive_identifier`` and ``reveal_secret`` — gated decrypts.

Both tools decrypt Fernet-encrypted family identifiers and pass them
back to the LLM, but ONLY when the speaker satisfies the household
privacy matrix in :mod:`api.ai.authz` (self / spouse / direct parent
of the subject ALLOW; everyone else DENY). Every call — allow or
deny — is audit-logged so a future review can answer "did Avi ever
read X's SSN, and who asked?".

Two tools:

* ``reveal_sensitive_identifier`` is the narrow original — SSN / tax
  ID stored on ``sensitive_identifiers`` rows.
* ``reveal_secret`` is the umbrella umbrella covering vehicle VIN /
  plate, identity-document number (driver's licence, passport, state
  ID), bank account & routing numbers, insurance policy numbers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .... import models
from ....crypto import decrypt_str
from ... import authz
from .._registry import ToolContext, ToolError


logger = logging.getLogger(__name__)


# ---- reveal_sensitive_identifier --------------------------------------


REVEAL_SENSITIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the family member whose identifier should "
                "be revealed. Use lookup_person first if you only know "
                "their name."
            ),
        },
        "identifier_type": {
            "type": "string",
            "description": (
                "Which identifier to reveal — typically "
                "'social_security_number'. Other values are stored as-is "
                "in sensitive_identifiers.identifier_type."
            ),
        },
    },
    "required": ["person_id", "identifier_type"],
}


async def handle_reveal_sensitive(
    ctx: ToolContext, person_id: int, identifier_type: str
) -> Dict[str, Any]:
    """Decrypt and return a person's SSN / tax ID — gated by relationship.

    This is the ONLY path that yields a full plaintext SSN. The check
    matches the user-stated rule: a person can always read their own,
    a parent can read their direct child's, spouses can read each
    other's, and everyone else (children, grandparents, siblings,
    in-laws, anonymous speakers) is denied. Every call — allow or
    deny — is logged via :mod:`ai.authz` so a future audit can answer
    "did Avi ever read Sarah's SSN, and who asked?".
    """
    if ctx.person_id is None and not ctx.is_admin:
        # An anonymous speaker (no identified person AND not admin)
        # can never decrypt anything. We refuse without even leaking
        # which subject was queried.
        raise ToolError(
            "I can't reveal sensitive identifiers without first "
            "knowing who is asking. Please greet me on camera (or "
            "email me from your registered address) and try again."
        )

    decision = authz.can_access_sensitive(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=int(person_id),
        requestor_is_admin=ctx.is_admin,
    )
    if not decision.allowed:
        # Rule #3: when the speaker is NOT self / spouse / direct
        # parent, we still want a warm, helpful response — not a
        # stonewall. The error string below is what the LLM sees as
        # tool output; phrase it as a coaching note so the model
        # composes a friendly reply that pivots to public info
        # (name, age, role, interests) instead of refusing flat.
        raise ToolError(
            "Don't share this encrypted identifier — the speaker isn't "
            "the subject, their spouse, or a direct parent of the "
            "subject, so household privacy rules keep this field "
            "private. BUT keep the conversation warm: respond in one "
            "or two friendly sentences that (a) acknowledge the "
            "request without sounding bureaucratic and (b) offer the "
            "public info you DO have about that person from the "
            "household-context block (their name, age, role in the "
            "family, hobbies / interests, what they're working on). "
            "Do NOT lecture about privacy rules; do NOT mention "
            "'household privacy gate' or quote this message verbatim. "
            "Suggest they ask the person themselves, their spouse, or "
            "a parent if they truly need the encrypted value."
        )

    rows = (
        ctx.db.query(models.SensitiveIdentifier)
        .filter(
            models.SensitiveIdentifier.person_id == int(person_id),
            models.SensitiveIdentifier.identifier_type == identifier_type,
        )
        .all()
    )
    if not rows:
        return {
            "found": False,
            "person_id": int(person_id),
            "identifier_type": identifier_type,
        }

    # If somehow there are multiple, return them all (e.g. someone has
    # both an SSN and a historical ITIN under the same type — unlikely
    # but cheap to support).
    revealed: List[Dict[str, Any]] = []
    for row in rows:
        try:
            plaintext = decrypt_str(row.identifier_value_encrypted)
        except RuntimeError as e:
            # Likely a key-mismatch (rotated FA_ENCRYPTION_KEY without
            # re-encrypting). Don't bubble the secret-y exception text
            # to the model — keep the error generic.
            logger.error(
                "Failed to decrypt sensitive_identifier_id=%s: %s",
                row.sensitive_identifier_id,
                e,
            )
            raise ToolError(
                "Stored value couldn't be decrypted with the current "
                "encryption key — flag this to the household admin."
            ) from e
        revealed.append(
            {
                "sensitive_identifier_id": row.sensitive_identifier_id,
                "identifier_type": row.identifier_type,
                "value": plaintext,
                "last_four": row.identifier_last_four,
            }
        )

    logger.info(
        "[authz] DECRYPT requestor=%s subject=%s identifier_type=%s count=%d",
        ctx.person_id,
        int(person_id),
        identifier_type,
        len(revealed),
    )
    return {
        "found": True,
        "person_id": int(person_id),
        "identifier_type": identifier_type,
        "results": revealed,
        "access_label": decision.label,
    }


# ---- reveal_secret ----------------------------------------------------
#
# Umbrella decrypt-and-return tool for every other Fernet-encrypted
# family identifier: vehicle VIN / license plate, identity-document
# number (driver's licence, passport), bank account & routing numbers,
# insurance policy number. Same household-privacy matrix as
# ``reveal_sensitive_identifier``: self / spouse / direct parent of the
# subject ALLOW, everyone else DENY.
#
# We resolve a "subject person" per category so the same authz check
# can be applied uniformly:
#
#   vehicle_vin / vehicle_license_plate → vehicles.primary_driver_person_id
#                                         (NULL → household-shared,
#                                          allowed for any identified
#                                          family member of same family)
#   identity_document_number            → identity_documents.person_id
#   financial_account_number /          → financial_accounts.primary_holder_person_id
#   financial_routing_number
#   insurance_policy_number             → any covered person via
#                                         insurance_policy_people
#                                         (allowed if speaker can access
#                                         ANY of them, which means a
#                                         covered person, their spouse,
#                                         or their parent)
#
# Every call — allow or deny — is audit-logged through ai.authz so
# ``rg "[authz]"`` can answer "did Avi ever read the truck's full VIN,
# and who asked?".


REVEAL_SECRET_CATEGORIES = (
    "vehicle_vin",
    "vehicle_license_plate",
    "identity_document_number",
    "financial_account_number",
    "financial_routing_number",
    "insurance_policy_number",
)


REVEAL_SECRET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(REVEAL_SECRET_CATEGORIES),
            "description": (
                "Which encrypted field to decrypt. Pick one of: "
                "vehicle_vin (full 17-char VIN), "
                "vehicle_license_plate (full plate), "
                "identity_document_number (driver's license / "
                "passport / state ID number), "
                "financial_account_number (bank or brokerage account "
                "number), financial_routing_number (ABA routing "
                "number on a checking/savings account), "
                "insurance_policy_number (full policy number)."
            ),
        },
        "record_id": {
            "type": "integer",
            "description": (
                "Primary-key id of the row to decrypt. The id "
                "interpretation depends on category: vehicle_id for "
                "the two vehicle_* categories, identity_document_id "
                "for identity_document_number, financial_account_id "
                "for the two financial_* categories, "
                "insurance_policy_id for insurance_policy_number. "
                "Use sql_query to find the id first if you only have "
                "the make/model, the institution, etc."
            ),
        },
    },
    "required": ["category", "record_id"],
}


def _resolve_secret_subject(
    db: Session, *, category: str, record_id: int
) -> tuple[Optional[object], List[int], Optional[int], Optional[bytes], Optional[str]]:
    """Look up the row + return (row, candidate_subject_ids, family_id, ciphertext, label).

    ``candidate_subject_ids`` is the list of person_ids the household
    privacy gate runs against. The speaker passes the gate if they can
    access AT LEAST ONE of them (that's how household-shared assets
    like a family truck or a joint policy work — every covered person
    is a valid "owner" for authz purposes).

    An empty list means "household-shared, no specific owner" and the
    caller falls back to a same-family check.

    Returns ``(None, [], None, None, None)`` if no row exists for that
    id.
    """
    if category in ("vehicle_vin", "vehicle_license_plate"):
        row = db.get(models.Vehicle, int(record_id))
        if row is None:
            return None, [], None, None, None
        ciphertext = (
            row.vehicle_identification_number_encrypted
            if category == "vehicle_vin"
            else row.license_plate_number_encrypted
        )
        label = "VIN" if category == "vehicle_vin" else "license plate"
        owners = (
            [int(row.primary_driver_person_id)]
            if row.primary_driver_person_id is not None
            else []
        )
        return row, owners, int(row.family_id), ciphertext, label

    if category == "identity_document_number":
        row = db.get(models.IdentityDocument, int(record_id))
        if row is None:
            return None, [], None, None, None
        # IdentityDocument doesn't carry family_id directly — pull it
        # from the owning person.
        owner_person = db.get(models.Person, int(row.person_id))
        family_id = owner_person.family_id if owner_person else None
        return (
            row,
            [int(row.person_id)],
            family_id,
            row.document_number_encrypted,
            f"{row.document_type} number",
        )

    if category in ("financial_account_number", "financial_routing_number"):
        row = db.get(models.FinancialAccount, int(record_id))
        if row is None:
            return None, [], None, None, None
        ciphertext = (
            row.account_number_encrypted
            if category == "financial_account_number"
            else row.routing_number_encrypted
        )
        label = (
            "account number"
            if category == "financial_account_number"
            else "routing number"
        )
        owners = (
            [int(row.primary_holder_person_id)]
            if row.primary_holder_person_id is not None
            else []
        )
        return row, owners, int(row.family_id), ciphertext, label

    if category == "insurance_policy_number":
        row = db.get(models.InsurancePolicy, int(record_id))
        if row is None:
            return None, [], None, None, None
        covered_ids = [
            int(p.person_id)
            for p in db.query(models.InsurancePolicyPerson)
            .filter(models.InsurancePolicyPerson.insurance_policy_id == int(record_id))
            .all()
        ]
        return (
            row,
            covered_ids,
            int(row.family_id),
            row.policy_number_encrypted,
            "policy number",
        )

    raise ToolError(f"Unknown reveal_secret category: {category!r}")


async def handle_reveal_secret(
    ctx: ToolContext, category: str, record_id: int
) -> Dict[str, Any]:
    """Decrypt one Fernet-encrypted family identifier — gated by relationship.

    Mirrors :func:`handle_reveal_sensitive` but for the wider set of
    encrypted family identifiers (vehicle VIN / plate, identity-doc
    number, bank account & routing numbers, insurance policy number).
    The privacy matrix is identical: a person can always read their
    own, a direct parent can read a direct child's, spouses can read
    each other's, everyone else (children → parents, siblings,
    grandparents, in-laws, anonymous speakers) is denied.
    """
    if category not in REVEAL_SECRET_CATEGORIES:
        raise ToolError(
            f"Unknown category {category!r}. Allowed: "
            + ", ".join(REVEAL_SECRET_CATEGORIES)
            + "."
        )

    if ctx.person_id is None and not ctx.is_admin:
        raise ToolError(
            "I can't reveal that without first knowing who is asking. "
            "Please greet me on camera (or email me from your "
            "registered address) and try again."
        )

    row, candidate_subject_ids, family_id, ciphertext, label = (
        _resolve_secret_subject(
            ctx.db, category=category, record_id=int(record_id)
        )
    )
    if row is None:
        return {
            "found": False,
            "category": category,
            "record_id": int(record_id),
        }

    # Cross-family belt-and-suspenders. The speaker's family_id (from
    # ToolContext) must match the row's family — refuse otherwise so
    # one household can't read another's secrets even if a hallucinated
    # record_id happens to land on a valid row.
    if (
        ctx.family_id is not None
        and family_id is not None
        and int(ctx.family_id) != int(family_id)
    ):
        logger.info(
            "[authz] DENY  scope=secret requestor=%s category=%s "
            "record_id=%s reason=cross_family",
            ctx.person_id,
            category,
            int(record_id),
        )
        raise ToolError(
            "That record belongs to a different household — I can't "
            "reveal it."
        )

    decision_label: Optional[str] = None
    allowed = False

    if ctx.is_admin:
        # Operator override — admins bypass the household
        # relationship gate. Audit log records ``label='admin'``.
        allowed = True
        decision_label = "admin"
    elif candidate_subject_ids:
        # Standard subject-based check. Speaker passes if they can
        # access ANY one of the candidate subjects (covers joint
        # accounts and shared insurance policies).
        for subject_id in candidate_subject_ids:
            decision = authz.can_access_sensitive(
                ctx.db,
                requestor_person_id=ctx.person_id,
                subject_person_id=int(subject_id),
            )
            if decision.allowed:
                allowed = True
                decision_label = decision.label
                break
    else:
        # Household-shared asset (e.g. a family vehicle with no
        # primary_driver assigned). Allow any identified family member
        # of the same household to read it. We still audit-log so the
        # decision is recoverable.
        speaker = (
            ctx.db.get(models.Person, int(ctx.person_id))
            if ctx.person_id is not None
            else None
        )
        if (
            speaker is not None
            and family_id is not None
            and int(speaker.family_id) == int(family_id)
        ):
            allowed = True
            decision_label = "household_shared"
        logger.info(
            "[authz] %s scope=secret requestor=%s category=%s record_id=%s "
            "reason=%s",
            "ALLOW" if allowed else "DENY ",
            ctx.person_id,
            category,
            int(record_id),
            decision_label or "unauthorized",
        )

    if not allowed:
        # Rule #3: same friendly-pivot guidance as
        # handle_reveal_sensitive — keep the conversation warm,
        # answer with the public facts (make / model / nickname /
        # institution / policy type / etc.) we DO have for this
        # record, and only suggest who to ask for the encrypted
        # value rather than refusing flat.
        raise ToolError(
            "Don't share this encrypted value — the speaker isn't the "
            "subject, their spouse, or a direct parent of the "
            "subject, so household privacy rules keep this field "
            "private. BUT keep the conversation warm: answer in one "
            "or two friendly sentences that (a) acknowledge the "
            "request without sounding bureaucratic and (b) share the "
            "public info you DO have for this record from the "
            "household-context block (vehicle make/model/nickname, "
            "policy type/carrier, account institution/nickname, etc.) "
            "plus the matching `_last_four` helper if it's there. "
            "Do NOT lecture about privacy rules or quote this message "
            "verbatim. Suggest they ask the subject, their spouse, or "
            "a parent if they truly need the encrypted value."
        )

    if not ciphertext:
        return {
            "found": True,
            "category": category,
            "record_id": int(record_id),
            "value": None,
            "note": (
                "The row exists but no encrypted value is stored for "
                "this field — the household never recorded it."
            ),
            "access_label": decision_label,
        }

    try:
        plaintext = decrypt_str(ciphertext)
    except RuntimeError as e:
        logger.error(
            "Failed to decrypt %s record_id=%s: %s",
            category,
            int(record_id),
            e,
        )
        raise ToolError(
            "Stored value couldn't be decrypted with the current "
            "encryption key — flag this to the household admin."
        ) from e

    logger.info(
        "[authz] DECRYPT scope=secret requestor=%s category=%s "
        "record_id=%s subject_candidates=%s",
        ctx.person_id,
        category,
        int(record_id),
        candidate_subject_ids or "household_shared",
    )

    return {
        "found": True,
        "category": category,
        "record_id": int(record_id),
        "label": label,
        "value": plaintext,
        "access_label": decision_label,
    }
