"""Tool registry the AI agent loop can dispatch into.

A *tool* here is a small, well-typed Python function the LLM can call
to read data or take an action. We deliberately keep the registry tiny
and explicit (no class auto-discovery) so it's obvious in code review
exactly what Avi is allowed to do.

Each tool declares:

* ``name`` — what the model uses in ``tool_calls``.
* ``description`` — short natural-language hint used by the model.
* ``parameters`` — JSON Schema for the arguments. We also use it to
  validate inputs before running the handler so a hallucinated
  argument shape can't crash the executor.
* ``handler`` — the actual Python callable. Receives a
  :class:`ToolContext` as its first argument and the model-supplied
  arguments as keyword args.
* ``timeout_seconds`` — hard deadline. Anything that runs longer is
  cancelled and the tool result becomes an error.
* ``requires`` — capability flags the tool needs (e.g. "google"). The
  agent can refuse to advertise a tool when its capability is missing
  rather than letting the model call it and get a confusing error.

Adding a new tool: define a handler, wrap it in :class:`Tool`, and
register it in :func:`build_default_registry`. That's it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..crypto import decrypt_str
from ..integrations import google_oauth, telegram as telegram_api, twilio_sms
from ..integrations.gmail import GmailSendError, send_email
from ..integrations.google_calendar import (
    CalendarError,
    CalendarNotShared,
    PerCalendarBusy,
    busy_for_calendars,
    events_for_calendar,
    find_free_slots,
    list_upcoming_events,
    merge_busy_intervals,
)
from . import authz, sql_tool


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    db: Session
    family_id: int
    assistant_id: Optional[int] = None
    person_id: Optional[int] = None  # who is talking, when known


@dataclass
class ToolResult:
    """Structured outcome we feed back to the model after each call."""

    ok: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    # Optional human-friendly summary for the UI step card. The full
    # ``output`` JSON also goes to the audit row; this is just the
    # one-line "Sent message gAAA…=" displayed inline.
    summary: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Trim ``output`` for the model — keep it short to save tokens."""
        if self.ok:
            return {"ok": True, "output": _truncate_for_model(self.output)}
        return {"ok": False, "error": self.error}


def _truncate_for_model(value: Any, *, limit: int = 4000) -> Any:
    """Stringify + cap large outputs so we don't blow the context window."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > limit:
            return value[: limit - 12] + "…(truncated)"
        return value
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 12] + "…(truncated)"
    return text


ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)
    handler: ToolHandler
    timeout_seconds: float = 15.0
    requires: tuple[str, ...] = field(default_factory=tuple)
    # Human-friendly title for capability descriptions ("Send email"
    # rather than "gmail_send"). Falls back to ``name`` when omitted.
    label: Optional[str] = None
    # Sample user phrasings the LLM can quote back when asked "what
    # can you do?". Two-to-three short examples per tool keeps the
    # system prompt small while still being concrete.
    examples: tuple[str, ...] = field(default_factory=tuple)

    def display_label(self) -> str:
        return self.label or self.name

    def to_ollama_schema(self) -> Dict[str, Any]:
        """Render the tool in the Ollama / OpenAI ``tools`` shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Capability-aware collection of :class:`Tool` instances."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def for_capabilities(self, available: set[str]) -> List[Tool]:
        """Return only tools whose ``requires`` are satisfied."""
        out: List[Tool] = []
        for t in self._tools.values():
            if all(req in available for req in t.requires):
                out.append(t)
        return out

    def to_ollama_tools(self, available: set[str]) -> List[Dict[str, Any]]:
        return [t.to_ollama_schema() for t in self.for_capabilities(available)]

    async def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Run a tool by name with timeout + structured error capture."""
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"Unknown tool {name!r}. Available: {sorted(self._tools)}",
            )
        # Light validation — mostly a "did the model send a dict?" check.
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} expects an arguments object, got {type(arguments).__name__}",
            )
        # Validate required fields.
        required = tool.parameters.get("required", [])
        missing = [k for k in required if k not in arguments]
        if missing:
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} missing required argument(s): {missing}",
            )

        started = time.monotonic()
        try:
            result_value = await asyncio.wait_for(
                tool.handler(ctx, **arguments),
                timeout=tool.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                error=(
                    f"Tool {name!r} timed out after {tool.timeout_seconds:.0f}s"
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ToolError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - bubble as structured error
            logger.exception("Tool %r crashed", name)
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result_value, ToolResult):
            result_value.duration_ms = result_value.duration_ms or duration_ms
            return result_value
        return ToolResult(ok=True, output=result_value, duration_ms=duration_ms)


class ToolError(RuntimeError):
    """Raised inside a tool handler to surface a clean, user-facing error
    to the agent loop without dumping a traceback."""


# ---------------------------------------------------------------------------
# Concrete tools
# ---------------------------------------------------------------------------


# ---- sql.query --------------------------------------------------------


_SQL_QUERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "A single read-only SELECT (or WITH ... SELECT). Always "
                "scope by family_id where the table has it. Avoid SELECT *."
            ),
        }
    },
    "required": ["sql"],
}


async def _handle_sql_query(ctx: ToolContext, sql: str) -> Dict[str, Any]:
    # Compute the speaker's accessible-subject window once per call so
    # the SQL sanitiser can redact partially-sensitive columns
    # (currently ``people.notes``) on the way back. Fully sensitive
    # tables are blocked at the parser; this handles column-level
    # cases like notes.
    scope = authz.build_speaker_scope(ctx.db, speaker_person_id=ctx.person_id)
    try:
        result = sql_tool.run_safe_query(
            ctx.db,
            sql,
            family_id=ctx.family_id,
            max_rows=50,
            accessible_subject_ids=scope.can_access_subject_ids,
        )
    except sql_tool.SqlToolError as e:
        raise ToolError(str(e)) from e
    return {
        "row_count": result.row_count,
        "truncated": result.truncated,
        "columns": result.columns,
        "rows": result.rows,
    }


# ---- lookup_person ----------------------------------------------------


_LOOKUP_PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Name (or partial name / nickname) of the family member to "
                "look up. Matches first_name, preferred_name, and last_name "
                "case-insensitively."
            ),
        }
    },
    "required": ["name"],
}


async def _handle_lookup_person(ctx: ToolContext, name: str) -> List[Dict[str, Any]]:
    """Quick fuzzy lookup of a household member by name.

    Faster, more reliable, and more privacy-respecting than asking the
    model to write SQL for the trivial 'who is Sarah' case. Returns up
    to 5 matches with the columns the model needs to take action.
    """
    needle = (name or "").strip()
    if not needle:
        return []
    pattern = f"%{needle.lower()}%"
    rows = (
        ctx.db.query(models.Person)
        .filter(models.Person.family_id == ctx.family_id)
        .all()
    )
    matches: List[Dict[str, Any]] = []
    for p in rows:
        haystack = " ".join(
            x for x in (p.first_name, p.preferred_name, p.last_name) if x
        ).lower()
        if pattern.strip("%") in haystack:
            matches.append(
                {
                    "person_id": p.person_id,
                    "first_name": p.first_name,
                    "preferred_name": p.preferred_name,
                    "last_name": p.last_name,
                    "email_address": p.email_address,
                    "work_email": p.work_email,
                    "gender": p.gender,
                }
            )
            if len(matches) >= 5:
                break
    return matches


# ---- reveal_sensitive_identifier --------------------------------------


_REVEAL_SENSITIVE_SCHEMA: Dict[str, Any] = {
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


async def _handle_reveal_sensitive(
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
    if ctx.person_id is None:
        # An anonymous speaker can never decrypt anything. We refuse
        # without even leaking which subject was queried.
        raise ToolError(
            "I can't reveal sensitive identifiers without first "
            "knowing who is asking. Please greet me on camera (or "
            "email me from your registered address) and try again."
        )

    decision = authz.can_access_sensitive(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=int(person_id),
        family_id=ctx.family_id,
    )
    if not decision.allowed:
        raise ToolError(
            "I can't share that — household privacy rules only let a "
            "person see their own sensitive identifiers, plus those of "
            "their spouse and direct children. Please ask the person "
            "themselves (or one of their parents) for it."
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


_REVEAL_SECRET_CATEGORIES = (
    "vehicle_vin",
    "vehicle_license_plate",
    "identity_document_number",
    "financial_account_number",
    "financial_routing_number",
    "insurance_policy_number",
)


_REVEAL_SECRET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": list(_REVEAL_SECRET_CATEGORIES),
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


async def _handle_reveal_secret(
    ctx: ToolContext, category: str, record_id: int
) -> Dict[str, Any]:
    """Decrypt one Fernet-encrypted family identifier — gated by relationship.

    Mirrors :func:`_handle_reveal_sensitive` but for the wider set of
    encrypted family identifiers (vehicle VIN / plate, identity-doc
    number, bank account & routing numbers, insurance policy number).
    The privacy matrix is identical: a person can always read their
    own, a direct parent can read a direct child's, spouses can read
    each other's, everyone else (children → parents, siblings,
    grandparents, in-laws, anonymous speakers) is denied.
    """
    if category not in _REVEAL_SECRET_CATEGORIES:
        raise ToolError(
            f"Unknown category {category!r}. Allowed: "
            + ", ".join(_REVEAL_SECRET_CATEGORIES)
            + "."
        )

    if ctx.person_id is None:
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

    if candidate_subject_ids:
        # Standard subject-based check. Speaker passes if they can
        # access ANY one of the candidate subjects (covers joint
        # accounts and shared insurance policies).
        for subject_id in candidate_subject_ids:
            decision = authz.can_access_sensitive(
                ctx.db,
                requestor_person_id=ctx.person_id,
                subject_person_id=int(subject_id),
                family_id=ctx.family_id,
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
        speaker = ctx.db.get(models.Person, int(ctx.person_id))
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
        raise ToolError(
            "I can't share that — household privacy rules only let a "
            "person see their own sensitive details, plus those of "
            "their spouse and direct children. Please ask the person "
            "themselves (or one of their parents) for it."
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


# ---- gmail.send -------------------------------------------------------


_GMAIL_SEND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient email address (one).",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": (
                "Plain-text email body. Sign off naturally as the assistant; "
                "do not include the recipient's name in the signature."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}


async def _handle_gmail_send(
    ctx: ToolContext, to: str, subject: str, body: str
) -> ToolResult:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — connect one in the admin UI."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    try:
        message_id = await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as e:
        raise ToolError(str(e)) from e

    return ToolResult(
        ok=True,
        output={"message_id": message_id, "to": to, "subject": subject},
        summary=f"Sent “{subject}” to {to}",
    )


# ---- calendar.list_upcoming -------------------------------------------


_CALENDAR_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "hours_ahead": {
            "type": "integer",
            "description": "How many hours into the future to scan. Default 72.",
            "minimum": 1,
            "maximum": 720,
        },
        "max_results": {
            "type": "integer",
            "description": "Max events to return across all calendars. Default 15.",
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": [],
}


async def _handle_calendar_list(
    ctx: ToolContext,
    hours_ahead: int = 72,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e
    try:
        events = await asyncio.to_thread(
            list_upcoming_events,
            creds,
            hours_ahead=hours_ahead,
            max_results=max_results,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e
    return [
        {
            "summary": e.summary,
            "start": e.start,
            "end": e.end,
            "location": e.location,
            "calendar_id": e.calendar_id,
        }
        for e in events
    ]


# ---- shared helpers for the per-person freebusy tools -----------------


def _looks_like_email(value: str) -> bool:
    """Lightweight email heuristic — good enough to branch the resolver."""
    return "@" in value and "." in value.split("@", 1)[1]


def _resolve_person_calendars(
    ctx: ToolContext, person: str
) -> tuple[Optional[models.Person], List[tuple[str, str]]]:
    """Turn a free-form ``person`` arg into ``(Person row, [(calendar_id, label), …])``.

    Returns BOTH the personal and work calendar ids (when populated)
    so the freebusy / event tools can hit them in a single Google
    request. The label is one of:

    * ``"personal"`` — ``Person.email_address``
    * ``"work"``     — ``Person.work_email``
    * ``"direct"``   — the caller passed an email address that
      didn't match any family member (we still try it as a
      single calendar so the agent isn't useless against a
      babysitter / handyman shared calendar).

    The returned list preserves order: personal first, work second.
    Callers can iterate it for the "personal calendar shows event
    titles, work calendar usually only shows busy" rendering rule.
    """
    needle = (person or "").strip()
    if not needle:
        return None, []

    if _looks_like_email(needle):
        # Match against EITHER personal or work email so "ben@work.io"
        # still resolves to the person row + pulls in their personal
        # calendar too.
        lowered = needle.lower()
        match = (
            ctx.db.query(models.Person)
            .filter(models.Person.family_id == ctx.family_id)
            .filter(
                models.Person.email_address.ilike(needle)
                | models.Person.work_email.ilike(needle)
            )
            .first()
        )
        if match is None:
            return None, [(needle, "direct")]
        return match, _calendar_pairs_for(match, requested_email=lowered)

    rows = (
        ctx.db.query(models.Person)
        .filter(models.Person.family_id == ctx.family_id)
        .all()
    )
    pattern = needle.lower()
    matches: List[models.Person] = []
    for p in rows:
        haystack = " ".join(
            x for x in (p.first_name, p.preferred_name, p.last_name) if x
        ).lower()
        if pattern in haystack:
            matches.append(p)
    if not matches:
        return None, []
    # Prefer an exact first-name / preferred-name match when there are
    # multiple hits ("Sam" → Sam, not Samantha) so a kid's question
    # doesn't accidentally pull a parent.
    exact = [
        p
        for p in matches
        if (p.first_name or "").lower() == pattern
        or (p.preferred_name or "").lower() == pattern
    ]
    chosen = exact[0] if exact else matches[0]
    return chosen, _calendar_pairs_for(chosen)


def _calendar_pairs_for(
    person: models.Person, *, requested_email: Optional[str] = None
) -> List[tuple[str, str]]:
    """Return ``[(calendar_id, label), …]`` for the configured emails.

    When ``requested_email`` is provided AND it matches one of the
    person's emails, that one is placed first so a "Is X's work
    calendar free?" style ask still feels targeted; the other is
    appended (so we still merge in the rest for completeness).
    """
    pairs: List[tuple[str, str]] = []
    personal = (person.email_address or "").strip()
    work = (person.work_email or "").strip()
    requested_l = (requested_email or "").lower()

    if personal:
        pairs.append((personal, "personal"))
    if work:
        pairs.append((work, "work"))

    if requested_l:
        pairs.sort(key=lambda p: 0 if p[0].lower() == requested_l else 1)
    return pairs


# Back-compat shim: a couple of (older) callers import the previous
# single-email helper. Returns the personal email so existing code
# keeps working unchanged; new calendar code paths should use
# :func:`_resolve_person_calendars`.
def _resolve_person_email(
    ctx: ToolContext, person: str
) -> tuple[Optional[models.Person], Optional[str]]:
    p, pairs = _resolve_person_calendars(ctx, person)
    return p, (pairs[0][0] if pairs else None)


def _parse_iso_arg(label: str, value: str) -> datetime:
    """Parse an LLM-supplied ISO 8601 timestamp with a friendly error."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError(f"{label} is required (ISO 8601 datetime).")
    try:
        # Accept trailing 'Z' as UTC (Python pre-3.11 datetime is fussy).
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ToolError(
            f"{label} must be ISO 8601 (e.g. 2026-04-20T09:00:00-04:00). "
            f"Got {value!r}."
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _humanize_iso(value: str) -> str:
    """Turn an RFC3339 timestamp into a short human-friendly string."""
    try:
        dt = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return value
    return dt.strftime("%a %b %-d %-I:%M %p %Z").rstrip()


# ---- calendar.check_availability --------------------------------------


_CALENDAR_CHECK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name (e.g. 'Ben') or email address of the household "
                "member whose schedule you want to check. Names are "
                "matched fuzzy against first / preferred / last."
            ),
        },
        "start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the window (e.g. "
                "2026-04-20T13:00:00-04:00). Include the timezone "
                "offset that matches the user's intent."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the window. Must be after start."
            ),
        },
    },
    "required": ["person", "start", "end"],
}


async def _handle_calendar_check_availability(
    ctx: ToolContext, person: str, start: str, end: str
) -> Dict[str, Any]:
    """Answer 'is X free between A and B?' across BOTH of X's calendars.

    Resolves ``person`` to their personal AND work emails, runs a
    single freebusy query against both, and returns:

    * ``per_calendar`` — one entry per calendar with shared/busy and
      the label (``personal`` / ``work`` / ``direct``). Lets the
      model say "His personal calendar isn't shared, but his work
      calendar shows him busy 2-3."
    * ``busy`` — the merged busy intervals across all SHARED
      calendars (sorted, overlap-merged). The "is X free?" answer
      should use this list — they're free if it's empty.
    * ``summary`` — a short natural-language phrasing the model can
      crib for its reply. Mentions any calendar that wasn't shared
      so the user knows to ask for that share.

    Free/busy intervals carry NO event detail — no titles, no
    locations — so we don't apply the calendar-detail relationship
    gate here. Anyone in the household can see whether anyone else
    is free or busy. Detail-level access is gated separately by
    :func:`calendar_list_for_person`.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile in the admin "
            "console (or pass an email address directly) and I can "
            "check their calendar."
        )

    start_dt = _parse_iso_arg("start", start)
    end_dt = _parse_iso_arg("end", end)
    if end_dt <= start_dt:
        raise ToolError("end must be strictly after start.")

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    display_name = (
        person_row.preferred_name or person_row.first_name
        if person_row
        else calendar_pairs[0][0]
    )

    try:
        per_cal = await asyncio.to_thread(
            busy_for_calendars,
            creds,
            calendars=calendar_pairs,
            start=start_dt,
            end=end_dt,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e

    merged_busy = merge_busy_intervals(per_cal)
    summary = _summarise_availability(display_name, per_cal, merged_busy)

    return {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": any(b.shared for b in per_cal),
        "busy": merged_busy,
        "summary": summary,
    }


def _per_cal_payload(b: PerCalendarBusy) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "calendar_id": b.calendar_id,
        "label": b.label,
        "shared": b.shared,
    }
    if b.shared:
        out["busy"] = b.busy
    else:
        out["reason"] = b.reason
    return out


def _summarise_availability(
    display_name: str,
    per_cal: List[PerCalendarBusy],
    merged_busy: List[dict],
) -> str:
    shared = [b for b in per_cal if b.shared]
    not_shared = [b for b in per_cal if not b.shared]

    if not shared:
        labels = ", ".join(f"{b.label} ({b.calendar_id})" for b in per_cal)
        return (
            f"None of {display_name}'s calendars are shared with me "
            f"({labels}). Ask them to share at least one with this "
            "assistant under Google Calendar → Settings → Share with "
            "specific people."
        )

    if not merged_busy:
        head = f"{display_name} is free across the entire window."
    else:
        first = merged_busy[0]
        more = len(merged_busy) - 1
        head = (
            f"{display_name} is busy "
            f"{_humanize_iso(first['start'])} – {_humanize_iso(first['end'])}"
            + (f" (and {more} more conflict(s))." if more > 0 else ".")
        )

    if not_shared:
        unshared_labels = ", ".join(b.label for b in not_shared)
        head += (
            f" Note: their {unshared_labels} calendar isn't shared "
            "with me, so this only reflects the calendars I can see."
        )
    return head


# ---- calendar.find_free_slots -----------------------------------------


_CALENDAR_FREE_SLOTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member to find time for."
            ),
        },
        "window_start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the search window (typically the "
                "earliest the user is willing to consider, e.g. tomorrow "
                "morning at 9am local time)."
            ),
        },
        "window_end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the search window (e.g. end of next "
                "week)."
            ),
        },
        "duration_minutes": {
            "type": "integer",
            "description": "Length of the desired free slot. Default 30.",
            "minimum": 5,
            "maximum": 480,
        },
        "working_hours_only": {
            "type": "boolean",
            "description": (
                "When true (default), only suggest slots between 9am "
                "and 6pm local time on each day. Set false for "
                "evenings / weekends explicitly."
            ),
        },
        "max_slots": {
            "type": "integer",
            "description": "Max number of suggestions. Default 5.",
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["person", "window_start", "window_end"],
}


async def _handle_calendar_find_free_slots(
    ctx: ToolContext,
    person: str,
    window_start: str,
    window_end: str,
    duration_minutes: int = 30,
    working_hours_only: bool = True,
    max_slots: int = 5,
) -> Dict[str, Any]:
    """Suggest open windows in a person's calendar.

    Queries BOTH the personal and work calendars (when configured),
    merges their busy intervals, and feeds the union into the pure
    :func:`find_free_slots` helper. A slot is only "free" if BOTH
    calendars are free at that time — exactly what you want when
    booking around someone's day job.

    If a calendar exists on the person's profile but isn't shared
    with the assistant, we still return slots from the calendars we
    CAN see and warn in ``summary`` so the user knows the
    suggestions might miss a hidden conflict.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile in the admin "
            "console (or pass an email address directly) and I can "
            "suggest a time."
        )

    start_dt = _parse_iso_arg("window_start", window_start)
    end_dt = _parse_iso_arg("window_end", window_end)
    if end_dt <= start_dt:
        raise ToolError("window_end must be strictly after window_start.")
    if (end_dt - start_dt).total_seconds() > 31 * 24 * 3600:
        raise ToolError(
            "Window is too wide — please limit to about a month of "
            "search time per call so the suggestions stay useful."
        )

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    display_name = (
        person_row.preferred_name or person_row.first_name
        if person_row
        else calendar_pairs[0][0]
    )

    try:
        per_cal = await asyncio.to_thread(
            busy_for_calendars,
            creds,
            calendars=calendar_pairs,
            start=start_dt,
            end=end_dt,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e

    if not any(b.shared for b in per_cal):
        labels = ", ".join(f"{b.label} ({b.calendar_id})" for b in per_cal)
        return {
            "person": display_name,
            "per_calendar": [_per_cal_payload(b) for b in per_cal],
            "any_shared": False,
            "slots": [],
            "summary": (
                f"None of {display_name}'s calendars ({labels}) are "
                f"shared with me, so I can't suggest free times. Ask "
                f"them to share at least one with "
                f"{_assistant_email(ctx) or 'this assistant'} under "
                "Google Calendar → Settings → Share with specific "
                "people."
            ),
        }

    merged_busy = merge_busy_intervals(per_cal)

    # Honour the original ISO offset so working-hours-only suggestions
    # land in the user's local day, not UTC.
    local_tz = start_dt.tzinfo or timezone.utc
    slots = find_free_slots(
        busy=merged_busy,
        window_start=start_dt,
        window_end=end_dt,
        duration_minutes=duration_minutes,
        working_hours=(9, 18) if working_hours_only else None,
        max_slots=max_slots,
        tz=local_tz,
    )

    not_shared = [b for b in per_cal if not b.shared]
    if not slots:
        summary = (
            f"I couldn't find a {duration_minutes}-minute slot for "
            f"{display_name} in that window — they look booked through "
            "it. Try widening the window or relaxing working_hours_only."
        )
    else:
        first = slots[0]
        summary = (
            f"Suggested time for {display_name}: "
            f"{_humanize_iso(first['start'])} – "
            f"{_humanize_iso(first['end'])}"
            + (
                f" (plus {len(slots) - 1} more option(s))."
                if len(slots) > 1
                else "."
            )
        )
    if not_shared:
        unshared_labels = ", ".join(b.label for b in not_shared)
        summary += (
            f" Heads up: their {unshared_labels} calendar isn't "
            "shared with me, so a hidden conflict on it could still "
            "land in one of these suggested slots."
        )

    return {
        "person": display_name,
        "per_calendar": [_per_cal_payload(b) for b in per_cal],
        "any_shared": True,
        "duration_minutes": duration_minutes,
        "working_hours_only": working_hours_only,
        "slots": slots,
        "summary": summary,
    }


# ---- calendar.list_for_person -----------------------------------------


_CALENDAR_LIST_FOR_PERSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": (
                "Name or email of the household member whose events "
                "you want to list."
            ),
        },
        "window_start": {
            "type": "string",
            "description": (
                "ISO 8601 start of the window (e.g. start of "
                "this week)."
            ),
        },
        "window_end": {
            "type": "string",
            "description": (
                "ISO 8601 end of the window (e.g. end of "
                "this week)."
            ),
        },
        "max_results_per_calendar": {
            "type": "integer",
            "description": (
                "Cap on events returned per calendar. Default 25."
            ),
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["person", "window_start", "window_end"],
}


async def _handle_calendar_list_for_person(
    ctx: ToolContext,
    person: str,
    window_start: str,
    window_end: str,
    max_results_per_calendar: int = 25,
) -> Dict[str, Any]:
    """List events on a person's personal + work calendars.

    Applies the calendar-detail relationship gate
    (:func:`authz.can_see_calendar_details`):

    * The speaker IS the subject, OR is the subject's spouse —
      events come back with full detail (summary, location,
      organizer, calendar label).
    * Anyone else (parents, children, siblings, in-laws, anonymous
      speakers) — events come back with summary/location replaced
      by ``[busy — private]`` and only their start / end / calendar
      label exposed. The reader still sees WHEN the person is
      busy but NOT what they're doing.

    A calendar that exists on the profile but isn't shared with
    the assistant comes back as ``shared=False`` with a hint to
    ask for the share.
    """
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    person_row, calendar_pairs = _resolve_person_calendars(ctx, person)
    if not calendar_pairs:
        raise ToolError(
            f"I don't have a personal or work email on file for "
            f"{person!r}. Add one to their profile and I can list "
            "their calendar."
        )
    if person_row is None:
        # Direct-email lookup against a non-family calendar — refuse
        # to list events: we can't run the relationship gate without
        # a Person, and silently leaking detail would be wrong.
        raise ToolError(
            f"I can only list calendar events for registered family "
            f"members. {person!r} isn't one of them — try giving me "
            "their name as it appears in the admin console."
        )

    start_dt = _parse_iso_arg("window_start", window_start)
    end_dt = _parse_iso_arg("window_end", window_end)
    if end_dt <= start_dt:
        raise ToolError("window_end must be strictly after window_start.")
    if (end_dt - start_dt).total_seconds() > 31 * 24 * 3600:
        raise ToolError(
            "Window is too wide — please limit to about a month per call."
        )

    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    detail_decision = authz.can_see_calendar_details(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=person_row.person_id,
        family_id=ctx.family_id,
    )
    show_detail = detail_decision.allowed
    display_name = person_row.preferred_name or person_row.first_name

    per_calendar_out: List[Dict[str, Any]] = []
    total_events = 0
    any_shared = False

    for cal_id, label in calendar_pairs:
        try:
            events = await asyncio.to_thread(
                events_for_calendar,
                creds,
                calendar_id=cal_id,
                start=start_dt,
                end=end_dt,
                max_results=max_results_per_calendar,
            )
        except CalendarNotShared as e:
            per_calendar_out.append(
                {
                    "calendar_id": cal_id,
                    "label": label,
                    "shared": False,
                    "reason": e.reason,
                }
            )
            continue
        except CalendarError as e:
            raise ToolError(str(e)) from e

        any_shared = True
        rendered: List[Dict[str, Any]] = []
        for ev in events:
            if show_detail:
                rendered.append(
                    {
                        "start": ev.start,
                        "end": ev.end,
                        "summary": ev.summary,
                        "location": ev.location,
                        "organizer_email": ev.organizer_email,
                    }
                )
            else:
                rendered.append(
                    {
                        "start": ev.start,
                        "end": ev.end,
                        "summary": "[busy — private]",
                        "location": None,
                        "organizer_email": None,
                    }
                )
        total_events += len(rendered)
        per_calendar_out.append(
            {
                "calendar_id": cal_id,
                "label": label,
                "shared": True,
                "events": rendered,
            }
        )

    if not any_shared:
        summary = (
            f"None of {display_name}'s calendars are shared with me. "
            f"Ask them to share at least one with "
            f"{_assistant_email(ctx) or 'this assistant'}."
        )
    elif total_events == 0:
        summary = (
            f"{display_name} has no events on their shared "
            "calendars in that window."
        )
    elif show_detail:
        summary = (
            f"{display_name} has {total_events} event(s) in that "
            "window — you (and their spouse) are allowed to see the "
            "full detail."
        )
    else:
        summary = (
            f"{display_name} has {total_events} busy slot(s) in that "
            "window. Per household privacy rules I'm only sharing "
            "free/busy with you, not the event titles. Ask "
            f"{display_name} themselves (or their spouse) for "
            "specifics."
        )

    return {
        "person": display_name,
        "show_detail": show_detail,
        "access_label": detail_decision.label,
        "per_calendar": per_calendar_out,
        "any_shared": any_shared,
        "total_events": total_events,
        "summary": summary,
    }


def _assistant_email(ctx: ToolContext) -> Optional[str]:
    """Best-effort lookup of the assistant's connected Google address."""
    if ctx.assistant_id is None:
        return None
    row = google_oauth.load_credentials_row(ctx.db, ctx.assistant_id)
    return getattr(row, "granted_email", None) if row else None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task board tools
#
# Avi is the household's "secretary" for the kanban — these handlers
# let her *create*, *list*, *read*, *update*, *comment on*, and
# *assign followers to* tasks without the LLM having to write SQL.
# Tasks are family-shared (no per-person privacy gate beyond the
# normal family scope) so the parameter shape is intentionally flat.
#
# When the LLM creates a task on behalf of a recognised speaker we
# pass ``ctx.person_id`` as ``created_by`` automatically — the model
# only needs to supply the title / description / priority. Same for
# comments: ``author_kind`` defaults to ``'assistant'`` when no
# ``author_person_id`` is given so Avi-authored notes are properly
# attributed in the audit trail.
# ---------------------------------------------------------------------------


_TASK_STATUSES_LIST = list(models.TASK_STATUSES)
_TASK_PRIORITIES_LIST = list(models.TASK_PRIORITIES)


def _serialize_task_for_model(t: models.Task) -> Dict[str, Any]:
    """Compact JSON shape returned to the LLM for a single task.

    Trimmed of the row's full description on list endpoints to keep
    the context window healthy — the model can call ``task_get`` for
    full detail when the user asks for it.
    """
    return {
        "task_id": t.task_id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "assigned_to_person_id": t.assigned_to_person_id,
        "created_by_person_id": t.created_by_person_id,
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "desired_end_date": (
            t.desired_end_date.isoformat() if t.desired_end_date else None
        ),
        "end_date": t.end_date.isoformat() if t.end_date else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


# ---- task_create ------------------------------------------------------


_TASK_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Short headline for the kanban card, e.g. 'Fix the east "
                "gate latch' or 'Renew Maddie's passport'. Required."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": (
                "Longer detail / acceptance criteria / context. Capture "
                "anything the user said that explains WHAT done looks "
                "like. Optional."
            ),
        },
        "priority": {
            "type": "string",
            "enum": _TASK_PRIORITIES_LIST,
            "description": (
                "One of urgent / high / normal / low / future_idea. "
                "Default to 'normal' unless the speaker tells you "
                "otherwise. Use 'future_idea' for casual 'someday' "
                "mentions so they don't pollute the active board."
            ),
        },
        "status": {
            "type": "string",
            "enum": _TASK_STATUSES_LIST,
            "description": (
                "Initial kanban column. Defaults to 'new'. Set to "
                "'in_progress' if the user is already mid-task."
            ),
        },
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": (
                "person_id of the owner. Defaults to the SPEAKER's "
                "person_id when omitted (the asker becomes the owner)."
            ),
        },
        "desired_end_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": (
                "Soft target the speaker wants the task done by, "
                "ISO YYYY-MM-DD. Use this when the user says 'by "
                "Friday', 'next week', etc."
            ),
        },
        "start_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": "When work is intended to begin (ISO date).",
        },
        "follower_person_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Other household members to loop in as followers. The "
                "creator + assignee are followers implicitly — only "
                "include EXTRAS here."
            ),
        },
    },
    "required": ["title"],
}


async def _handle_task_create(
    ctx: ToolContext,
    title: str,
    description: Optional[str] = None,
    priority: str = "normal",
    status: str = "new",
    assigned_to_person_id: Optional[int] = None,
    desired_end_date: Optional[str] = None,
    start_date: Optional[str] = None,
    follower_person_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    cleaned_title = (title or "").strip()
    if not cleaned_title:
        raise ToolError("Cannot create a task with an empty title.")
    if priority not in models.TASK_PRIORITIES:
        raise ToolError(
            f"priority must be one of {list(models.TASK_PRIORITIES)}, got {priority!r}"
        )
    if status not in models.TASK_STATUSES:
        raise ToolError(
            f"status must be one of {list(models.TASK_STATUSES)}, got {status!r}"
        )

    owner = assigned_to_person_id
    if owner is None and ctx.person_id is not None:
        owner = ctx.person_id

    if owner is not None:
        if (
            ctx.db.get(models.Person, owner) is None
            or ctx.db.query(models.Person)
            .filter(
                models.Person.person_id == owner,
                models.Person.family_id == ctx.family_id,
            )
            .first()
            is None
        ):
            raise ToolError(
                f"Assignee person_id={owner} is not a member of this family."
            )

    def _parse_date(label: str, raw: Optional[str]) -> Optional["date"]:
        if raw is None:
            return None
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError as exc:
            raise ToolError(f"{label} must be ISO YYYY-MM-DD, got {raw!r}") from exc

    from datetime import date  # local to avoid polluting module top-level

    task = models.Task(
        family_id=ctx.family_id,
        created_by_person_id=ctx.person_id,
        assigned_to_person_id=owner,
        title=cleaned_title,
        description=description,
        status=status,
        priority=priority,
        start_date=_parse_date("start_date", start_date),
        desired_end_date=_parse_date("desired_end_date", desired_end_date),
        completed_at=datetime.now(timezone.utc) if status == "done" else None,
    )
    ctx.db.add(task)
    ctx.db.flush()

    implicit = {p for p in (ctx.person_id, owner) if p is not None}
    for pid in follower_person_ids or []:
        if pid in implicit:
            continue
        if (
            ctx.db.query(models.Person)
            .filter(
                models.Person.person_id == pid,
                models.Person.family_id == ctx.family_id,
            )
            .first()
            is None
        ):
            raise ToolError(
                f"Follower person_id={pid} is not a member of this family."
            )
        ctx.db.add(
            models.TaskFollower(
                task_id=task.task_id,
                person_id=pid,
                added_at=datetime.now(timezone.utc),
            )
        )
        implicit.add(pid)

    ctx.db.flush()
    ctx.db.refresh(task)
    return {"created": _serialize_task_for_model(task)}


# ---- task_list --------------------------------------------------------


_TASK_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": (
                "Filter to tasks owned by this person_id. Use 0 for "
                "explicitly UNASSIGNED tasks. Omit to see everyone's."
            ),
        },
        "mine_only": {
            "type": "boolean",
            "description": (
                "Shortcut equivalent to assigned_to_person_id=<speaker>. "
                "Defaults to false. Set true when the user says 'my "
                "tasks' / 'what's on my plate'."
            ),
        },
        "priority": {
            "type": ["string", "null"],
            "enum": _TASK_PRIORITIES_LIST + [None],
            "description": (
                "Filter to a single priority bucket. Use repeated calls "
                "for 'urgent and high' answers."
            ),
        },
        "status": {
            "type": ["string", "null"],
            "enum": _TASK_STATUSES_LIST + [None],
            "description": "Filter to one kanban column.",
        },
        "include_done": {
            "type": "boolean",
            "description": (
                "Include status='done' tasks. Defaults to FALSE here so "
                "list calls focus on active work — set true when "
                "answering 'what did I close this week?'."
            ),
        },
        "q": {
            "type": ["string", "null"],
            "description": "Substring match against title or description.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max rows to return. Defaults to 15.",
        },
    },
    "required": [],
}


async def _handle_task_list(
    ctx: ToolContext,
    assigned_to_person_id: Optional[int] = None,
    mine_only: bool = False,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    include_done: bool = False,
    q: Optional[str] = None,
    limit: int = 15,
) -> Dict[str, Any]:
    from sqlalchemy import case, or_, select

    qry = select(models.Task).where(models.Task.family_id == ctx.family_id)
    if mine_only:
        if ctx.person_id is None:
            raise ToolError(
                "I don't know who's asking yet, so I can't filter to "
                "'your' tasks. Greet me on camera or email me from a "
                "registered address and try again."
            )
        qry = qry.where(models.Task.assigned_to_person_id == ctx.person_id)
    elif assigned_to_person_id is not None:
        if int(assigned_to_person_id) == 0:
            qry = qry.where(models.Task.assigned_to_person_id.is_(None))
        else:
            qry = qry.where(
                models.Task.assigned_to_person_id == int(assigned_to_person_id)
            )

    if status is not None:
        if status not in models.TASK_STATUSES:
            raise ToolError(
                f"status must be one of {list(models.TASK_STATUSES)}"
            )
        qry = qry.where(models.Task.status == status)
    elif not include_done:
        qry = qry.where(models.Task.status != "done")

    if priority is not None:
        if priority not in models.TASK_PRIORITIES:
            raise ToolError(
                f"priority must be one of {list(models.TASK_PRIORITIES)}"
            )
        qry = qry.where(models.Task.priority == priority)

    if q:
        like = f"%{q}%"
        qry = qry.where(
            or_(
                models.Task.title.ilike(like),
                models.Task.description.ilike(like),
            )
        )

    priority_rank = case(
        (models.Task.priority == "urgent", 0),
        (models.Task.priority == "high", 1),
        (models.Task.priority == "normal", 2),
        (models.Task.priority == "low", 3),
        (models.Task.priority == "future_idea", 4),
        else_=5,
    )
    status_rank = case(
        (models.Task.status == "in_progress", 0),
        (models.Task.status == "finalizing", 1),
        (models.Task.status == "new", 2),
        (models.Task.status == "done", 9),
        else_=5,
    )
    qry = qry.order_by(
        status_rank.asc(),
        priority_rank.asc(),
        models.Task.created_at.desc(),
    ).limit(min(max(int(limit), 1), 50))

    rows = list(ctx.db.execute(qry).scalars())
    return {
        "count": len(rows),
        "tasks": [_serialize_task_for_model(r) for r in rows],
    }


# ---- task_get ---------------------------------------------------------


_TASK_GET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer", "description": "tasks.task_id"},
    },
    "required": ["task_id"],
}


async def _handle_task_get(ctx: ToolContext, task_id: int) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    return {
        **_serialize_task_for_model(task),
        "description": task.description,
        "follower_person_ids": [f.person_id for f in task.followers],
        "comments": [
            {
                "task_comment_id": c.task_comment_id,
                "author_kind": c.author_kind,
                "author_person_id": c.author_person_id,
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
            for c in task.comments
        ],
        "attachment_count": len(task.attachments),
    }


# ---- task_update ------------------------------------------------------


_TASK_UPDATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "status": {
            "type": ["string", "null"],
            "enum": _TASK_STATUSES_LIST + [None],
        },
        "priority": {
            "type": ["string", "null"],
            "enum": _TASK_PRIORITIES_LIST + [None],
        },
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": "Set to null to unassign.",
        },
        "desired_end_date": {
            "type": ["string", "null"],
            "format": "date",
        },
        "start_date": {"type": ["string", "null"], "format": "date"},
        "end_date": {"type": ["string", "null"], "format": "date"},
    },
    "required": ["task_id"],
}


async def _handle_task_update(
    ctx: ToolContext,
    task_id: int,
    **fields: Any,
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")

    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in models.TASK_STATUSES:
            raise ToolError(
                f"status must be one of {list(models.TASK_STATUSES)}"
            )
        if fields["status"] == "done" and task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)
        elif fields["status"] != "done" and task.completed_at is not None:
            task.completed_at = None

    if "priority" in fields and fields["priority"] is not None:
        if fields["priority"] not in models.TASK_PRIORITIES:
            raise ToolError(
                f"priority must be one of {list(models.TASK_PRIORITIES)}"
            )

    if (
        "assigned_to_person_id" in fields
        and fields["assigned_to_person_id"] is not None
        and ctx.db.query(models.Person)
        .filter(
            models.Person.person_id == fields["assigned_to_person_id"],
            models.Person.family_id == ctx.family_id,
        )
        .first()
        is None
    ):
        raise ToolError(
            f"Assignee person_id={fields['assigned_to_person_id']} is not "
            "a member of this family."
        )

    for label in ("start_date", "desired_end_date", "end_date"):
        if label in fields and fields[label] is not None:
            try:
                fields[label] = datetime.fromisoformat(fields[label]).date()
            except ValueError as exc:
                raise ToolError(
                    f"{label} must be ISO YYYY-MM-DD, got {fields[label]!r}"
                ) from exc

    for k, v in fields.items():
        if hasattr(task, k):
            setattr(task, k, v)

    ctx.db.flush()
    ctx.db.refresh(task)
    return {"updated": _serialize_task_for_model(task)}


# ---- task_add_comment -------------------------------------------------


_TASK_ADD_COMMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "body": {
            "type": "string",
            "description": (
                "Comment text. Avi typically writes SHORT auto-notes "
                "(one or two sentences) — e.g. 'Marking this as done "
                "per Sarah's request.'"
            ),
        },
        "author_kind": {
            "type": "string",
            "enum": list(models.TASK_COMMENT_AUTHOR_KINDS),
            "description": (
                "'assistant' (default) when Avi is writing the note "
                "herself; 'person' when relaying a comment dictated by "
                "the speaker."
            ),
        },
    },
    "required": ["task_id", "body"],
}


async def _handle_task_add_comment(
    ctx: ToolContext,
    task_id: int,
    body: str,
    author_kind: str = "assistant",
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    if author_kind not in models.TASK_COMMENT_AUTHOR_KINDS:
        raise ToolError(
            f"author_kind must be one of {list(models.TASK_COMMENT_AUTHOR_KINDS)}"
        )
    body = (body or "").strip()
    if not body:
        raise ToolError("Comment body cannot be empty.")

    comment = models.TaskComment(
        task_id=task.task_id,
        author_person_id=ctx.person_id if author_kind == "person" else None,
        author_kind=author_kind,
        body=body,
        created_at=datetime.now(timezone.utc),
    )
    ctx.db.add(comment)
    ctx.db.flush()
    ctx.db.refresh(comment)
    return {
        "task_comment_id": comment.task_comment_id,
        "task_id": comment.task_id,
        "author_kind": comment.author_kind,
        "author_person_id": comment.author_person_id,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }


# ---- task_add_follower ------------------------------------------------


_TASK_ADD_FOLLOWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the family member to add as a follower. "
                "Use lookup_person first if you only have a name."
            ),
        },
    },
    "required": ["task_id", "person_id"],
}


async def _handle_task_add_follower(
    ctx: ToolContext, task_id: int, person_id: int
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    if (
        ctx.db.query(models.Person)
        .filter(
            models.Person.person_id == int(person_id),
            models.Person.family_id == ctx.family_id,
        )
        .first()
        is None
    ):
        raise ToolError(
            f"person_id={person_id} is not a member of this family."
        )

    existing = (
        ctx.db.query(models.TaskFollower)
        .filter(
            models.TaskFollower.task_id == task.task_id,
            models.TaskFollower.person_id == int(person_id),
        )
        .first()
    )
    if existing is not None:
        return {
            "task_follower_id": existing.task_follower_id,
            "task_id": existing.task_id,
            "person_id": existing.person_id,
            "already_following": True,
        }

    follower = models.TaskFollower(
        task_id=task.task_id,
        person_id=int(person_id),
        added_at=datetime.now(timezone.utc),
    )
    ctx.db.add(follower)
    ctx.db.flush()
    ctx.db.refresh(follower)
    return {
        "task_follower_id": follower.task_follower_id,
        "task_id": follower.task_id,
        "person_id": follower.person_id,
        "already_following": False,
    }


# ---- telegram_invite --------------------------------------------------
#
# Telegram bots cannot send the first message in a conversation — that
# rule is enforced server-side by Telegram, so we cannot just look up
# a household member's Telegram handle and message them out of the
# blue. The work-around is a deep-link invite:
#
#   1. Mint a one-time URL-safe token and persist it on a
#      ``telegram_invites`` row pointing at the invitee's Person.
#   2. Build the URL ``https://t.me/<bot_username>?start=<token>``
#      using the cached ``getMe`` lookup.
#   3. Deliver the URL to the invitee through a channel we already
#      own — preferring SMS when they have a ``mobile_phone_number``
#      on file, falling back to email via the assistant's connected
#      Gmail when they have an ``email_address``.
#   4. When the invitee taps the link, Telegram opens the bot with a
#      "Start" button pre-filled. Tapping Start delivers
#      ``/start <token>`` to the bot — exactly what
#      ``services.telegram_inbox._claim_telegram_invite`` consumes
#      to bind ``people.telegram_user_id`` and reply with a welcome.
#
# Authz: requires an identified speaker (so we have someone to
# attribute ``created_by_person_id`` to) and the invitee must belong
# to the same household. The household privacy matrix that gates
# secret-reveal tools doesn't apply here — onboarding a sibling /
# spouse / parent to Telegram is a routine household task, not a
# privileged data read.


_TELEGRAM_INVITE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the household member to invite. Use "
                "lookup_person first if you only have a name."
            ),
        },
        "channel": {
            "type": "string",
            "enum": ["auto", "sms", "email"],
            "description": (
                "How to deliver the invite link. 'auto' (default) "
                "prefers SMS when the person has a mobile phone on "
                "file, else email. Force one explicitly when the "
                "user asks 'text it to her' / 'email it to him'."
            ),
        },
    },
    "required": ["person_id"],
}


_TELEGRAM_INVITE_SMS_TEMPLATE = (
    "{assistant_name} here. Tap to start chatting with me on "
    "Telegram — your messages will reach me just like a phone "
    "call or email: {url}"
)


_TELEGRAM_INVITE_EMAIL_SUBJECT_TEMPLATE = (
    "Chat with {assistant_name} on Telegram"
)


_TELEGRAM_INVITE_EMAIL_BODY_TEMPLATE = (
    "Hi {invitee_name},\n\n"
    "{assistant_name} here. Tap the link below in Telegram to "
    "start chatting with me — your messages from then on will "
    "reach me directly, just like the family chat or an email:\n\n"
    "{url}\n\n"
    "If you don't have Telegram installed yet, you can grab it "
    "free from the App Store / Play Store first. The link expires "
    "on {expires_human}.\n\n"
    "— {assistant_name}"
)


async def _handle_telegram_invite(
    ctx: ToolContext,
    person_id: int,
    channel: str = "auto",
) -> ToolResult:
    settings = get_settings()
    if ctx.person_id is None:
        raise ToolError(
            "I can't issue a Telegram invite without first knowing "
            "who is asking. Greet me on camera (or message me from "
            "a registered email / phone / Telegram account) and try "
            "again."
        )
    if channel not in ("auto", "sms", "email"):
        raise ToolError(
            f"channel must be one of 'auto', 'sms', 'email'; got {channel!r}."
        )
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ToolError(
            "Telegram bot token isn't configured on this server "
            "(TELEGRAM_BOT_TOKEN missing) — I can't generate an "
            "invite link."
        )

    invitee = ctx.db.get(models.Person, int(person_id))
    if invitee is None or invitee.family_id != ctx.family_id:
        raise ToolError(
            f"person_id={person_id} is not a member of this family."
        )

    if invitee.telegram_user_id is not None:
        # No need for an invite — they're already linked. Surface a
        # clean error instead of silently churning a token the user
        # would never use.
        return ToolResult(
            ok=True,
            output={
                "already_linked": True,
                "person_id": invitee.person_id,
                "telegram_user_id": invitee.telegram_user_id,
                "telegram_username": invitee.telegram_username,
                "message": (
                    f"{invitee.preferred_name or invitee.first_name} "
                    "is already connected to me on Telegram — no "
                    "invite needed."
                ),
            },
            summary=(
                f"{invitee.preferred_name or invitee.first_name} is "
                "already on Telegram with me."
            ),
        )

    # Resolve the bot username for the deep link. Cached so repeated
    # calls (or many invites in quick succession) don't hammer the
    # Bot API.
    try:
        identity = await asyncio.to_thread(
            telegram_api.get_me_cached, settings.TELEGRAM_BOT_TOKEN
        )
    except telegram_api.TelegramReadError as exc:
        raise ToolError(f"Couldn't reach Telegram to mint invite: {exc}") from exc
    if not identity.username:
        raise ToolError(
            "Bot has no @username — open @BotFather and assign one, "
            "otherwise the invite URL can't be built."
        )

    chosen_channel, sent_to = _resolve_invite_channel(
        invitee=invitee, channel=channel
    )

    invite, reused = _find_or_mint_invite(
        ctx.db,
        invitee=invitee,
        created_by_person_id=ctx.person_id,
        channel=chosen_channel,
        sent_to=sent_to,
    )
    invite_url = telegram_api.build_invite_url(
        bot_username=identity.username,
        payload_token=invite.payload_token,
    )

    family = ctx.db.get(models.Family, ctx.family_id)
    assistant_name = (
        family.assistant.assistant_name
        if family and family.assistant
        else "Avi"
    )
    invitee_name = (
        invitee.preferred_name
        or invitee.first_name
        or "there"
    )
    expires_human = invite.expires_at.strftime("%A %B %-d, %Y")

    if chosen_channel == "sms":
        body = _TELEGRAM_INVITE_SMS_TEMPLATE.format(
            assistant_name=assistant_name,
            url=invite_url,
        )
        sid = await _send_invite_sms(
            settings=settings, to_phone=sent_to, body=body
        )
        delivery = {
            "sms_message_sid": sid,
            "sent_to_phone": sent_to,
        }
    else:
        subject = _TELEGRAM_INVITE_EMAIL_SUBJECT_TEMPLATE.format(
            assistant_name=assistant_name
        )
        body = _TELEGRAM_INVITE_EMAIL_BODY_TEMPLATE.format(
            invitee_name=invitee_name,
            assistant_name=assistant_name,
            url=invite_url,
            expires_human=expires_human,
        )
        message_id = await _send_invite_email(
            ctx=ctx, to=sent_to, subject=subject, body=body
        )
        delivery = {
            "gmail_message_id": message_id,
            "sent_to_email": sent_to,
            "subject": subject,
        }

    ctx.db.commit()

    return ToolResult(
        ok=True,
        output={
            "telegram_invite_id": invite.telegram_invite_id,
            "person_id": invitee.person_id,
            "channel": chosen_channel,
            "sent_to": sent_to,
            "invite_url": invite_url,
            "expires_at": invite.expires_at.isoformat(),
            "reused_outstanding_invite": reused,
            "delivery": delivery,
        },
        summary=(
            f"Sent {invitee_name} a Telegram invite via "
            f"{chosen_channel} ({sent_to})."
            + (" (reused outstanding invite)" if reused else "")
        ),
    )


def _resolve_invite_channel(
    *, invitee: models.Person, channel: str
) -> tuple[str, str]:
    """Pick the delivery channel and return ``(channel, destination)``."""
    has_phone = bool((invitee.mobile_phone_number or "").strip())
    has_email = bool((invitee.email_address or "").strip())

    if channel == "sms":
        if not has_phone:
            raise ToolError(
                f"{invitee.preferred_name or invitee.first_name} has "
                "no mobile_phone_number on file, so I can't text them "
                "the invite. Try channel='email' or add a phone in "
                "the admin console first."
            )
        return "sms", invitee.mobile_phone_number.strip()

    if channel == "email":
        if not has_email:
            raise ToolError(
                f"{invitee.preferred_name or invitee.first_name} has "
                "no email_address on file, so I can't email them the "
                "invite. Try channel='sms' or add an email in the "
                "admin console first."
            )
        return "email", invitee.email_address.strip()

    # auto
    if has_phone:
        return "sms", invitee.mobile_phone_number.strip()
    if has_email:
        return "email", invitee.email_address.strip()
    raise ToolError(
        f"{invitee.preferred_name or invitee.first_name} has neither "
        "a mobile phone nor an email on file, so there's no way for "
        "me to deliver a Telegram invite. Add one to their profile "
        "in the admin console and try again."
    )


def _find_or_mint_invite(
    db: Session,
    *,
    invitee: models.Person,
    created_by_person_id: int,
    channel: str,
    sent_to: str,
) -> tuple[models.TelegramInvite, bool]:
    """Reuse the active invite row for this person, or mint a fresh one.

    The partial-unique index ``uq_telegram_invites_active_per_person``
    enforces at most one ``(claimed_at IS NULL AND revoked_at IS
    NULL)`` row per person, so we always find at most one to reuse.
    If an existing row is still within its TTL we keep its token
    (so an old SMS the recipient might still have on their phone
    keeps working) and just update the audit fields. If it expired,
    we refresh ``expires_at`` to a new 30-day window — same effect:
    the previously-delivered link starts working again. Returns
    ``(invite, reused)`` so the model can phrase the reply
    accurately ("here's the link I sent earlier" vs "here's a fresh
    link").
    """
    from sqlalchemy import select as _select

    now = datetime.now(timezone.utc)
    active = db.execute(
        _select(models.TelegramInvite)
        .where(models.TelegramInvite.person_id == invitee.person_id)
        .where(models.TelegramInvite.claimed_at.is_(None))
        .where(models.TelegramInvite.revoked_at.is_(None))
        .order_by(models.TelegramInvite.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        active.sent_via = channel
        active.sent_to = sent_to
        if active.expires_at <= now:
            # Token had aged out — extend its life rather than
            # rotating, so the URL the household admin sent earlier
            # is still tappable.
            active.expires_at = now + models.TELEGRAM_INVITE_DEFAULT_TTL
        db.flush()
        return active, True

    invite = models.TelegramInvite(
        family_id=invitee.family_id,
        person_id=invitee.person_id,
        created_by_person_id=created_by_person_id,
        payload_token=models.generate_invite_token(),
        sent_via=channel,
        sent_to=sent_to,
        expires_at=now + models.TELEGRAM_INVITE_DEFAULT_TTL,
    )
    db.add(invite)
    db.flush()
    return invite, False


async def _send_invite_sms(*, settings, to_phone: str, body: str) -> str:
    if not (
        settings.TWILIO_ACCOUNT_SID
        and settings.TWILIO_AUTH_TOKEN
        and settings.TWILIO_PRIMARY_PHONE
    ):
        raise ToolError(
            "Twilio isn't configured (need TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN, TWILIO_PRIMARY_PHONE) so I can't "
            "text the invite. Try channel='email' instead."
        )
    try:
        return await asyncio.to_thread(
            twilio_sms.send_sms,
            account_sid=settings.TWILIO_ACCOUNT_SID,
            auth_token=settings.TWILIO_AUTH_TOKEN,
            from_phone=settings.TWILIO_PRIMARY_PHONE,
            to_phone=to_phone,
            body=body,
        )
    except twilio_sms.TwilioSendError as exc:
        raise ToolError(f"Twilio refused the invite SMS: {exc}") from exc


async def _send_invite_email(
    *, ctx: ToolContext, to: str, subject: str, body: str
) -> str:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — I can't "
            "send the invite by email."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as exc:
        raise ToolError(
            f"Google isn't connected for this assistant ({exc}) — "
            "try channel='sms' instead."
        ) from exc
    except google_oauth.GoogleOAuthError as exc:
        raise ToolError(f"Google auth error: {exc}") from exc
    try:
        return await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as exc:
        raise ToolError(f"Gmail refused the invite email: {exc}") from exc


def build_default_registry() -> ToolRegistry:
    """Construct the registry the chat agent uses by default."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="sql_query",
            label="Query the family database",
            description=(
                "Run a single read-only SELECT against the family database. "
                "Useful for ad-hoc lookups (vehicles, residences, insurance, "
                "etc.) when the prebuilt tools below don't fit. Always include "
                "family_id in the WHERE clause."
            ),
            parameters=_SQL_QUERY_SCHEMA,
            handler=_handle_sql_query,
            timeout_seconds=8.0,
            examples=(
                "How many cars do we own?",
                "When does our auto insurance renew?",
                "Who in the family takes blood pressure medication?",
            ),
        )
    )
    reg.register(
        Tool(
            name="lookup_person",
            label="Look up a family member",
            description=(
                "Find a household member by partial name. Returns person_id, "
                "names, email, and gender. Use this BEFORE drafting an email "
                "to a family member so you have their real address."
            ),
            parameters=_LOOKUP_PERSON_SCHEMA,
            handler=_handle_lookup_person,
            timeout_seconds=4.0,
            examples=(
                "What's Sarah's email address?",
                "Tell me about Ben.",
            ),
        )
    )
    reg.register(
        Tool(
            name="reveal_sensitive_identifier",
            label="Reveal a sensitive identifier (SSN, tax ID)",
            description=(
                "Decrypt and return a family member's full sensitive "
                "identifier (typically Social Security Number). The tool "
                "enforces relationship-based privacy: it ONLY returns a "
                "value when the speaker is the subject themselves, the "
                "subject's spouse, or one of the subject's direct "
                "parents. Children, grandparents, siblings, in-laws, "
                "and anonymous speakers are refused. Every call is "
                "audit-logged. Use this only when the user explicitly "
                "asks for the full number; otherwise stick to the "
                "*_last_four helper columns."
            ),
            parameters=_REVEAL_SENSITIVE_SCHEMA,
            handler=_handle_reveal_sensitive,
            timeout_seconds=5.0,
            examples=(
                "What's my SSN?",
                "Read me my daughter's social security number.",
            ),
        )
    )
    reg.register(
        Tool(
            name="reveal_secret",
            label="Reveal an encrypted family identifier (VIN, plate, account #, policy #, ID #)",
            description=(
                "Decrypt and return one Fernet-encrypted family "
                "identifier: a vehicle's full VIN or license plate, "
                "an identity-document number (driver's licence, "
                "passport, state ID), a bank account or routing "
                "number, or an insurance policy number. Enforces the "
                "same household privacy matrix as "
                "reveal_sensitive_identifier — it ONLY returns a "
                "value when the speaker is the subject themselves, "
                "the subject's spouse, or one of the subject's direct "
                "parents (so a child cannot read a parent's data, "
                "and a sibling cannot read a sibling's). For shared "
                "household assets like a vehicle with no primary "
                "driver, any identified family member of the same "
                "household may read it. Every call is audit-logged. "
                "Use this whenever the user explicitly asks for the "
                "FULL value of one of these fields — for everyday "
                "questions stick to the *_last_four helper columns "
                "you can already see via sql_query."
            ),
            parameters=_REVEAL_SECRET_SCHEMA,
            handler=_handle_reveal_secret,
            timeout_seconds=5.0,
            examples=(
                "What's the VIN on my truck?",
                "Read me my driver's license number.",
                "Tell me the policy number on our auto insurance.",
            ),
        )
    )
    reg.register(
        Tool(
            name="gmail_send",
            label="Send an email",
            description=(
                "Send a plain-text email from the assistant's connected "
                "Gmail account. Returns the Gmail message_id on success. "
                "ONLY call this once you have the recipient's real email "
                "address (use lookup_person first if needed) and a fully-"
                "drafted subject and body."
            ),
            parameters=_GMAIL_SEND_SCHEMA,
            handler=_handle_gmail_send,
            timeout_seconds=20.0,
            requires=("google",),
            examples=(
                "Send Mom a note thanking her for dinner.",
                "Email Ben a one-line summary of tomorrow's calendar.",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_upcoming",
            label="Read the calendar",
            description=(
                "List events on the assistant's connected Google calendar "
                "(and any calendars shared with it) for the next N hours. "
                "Use to answer 'what's coming up' or to gather context for "
                "an email about a future event."
            ),
            parameters=_CALENDAR_LIST_SCHEMA,
            handler=_handle_calendar_list,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on the calendar this week?",
                "Are we free Saturday afternoon?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_check_availability",
            label="Check a person's free/busy",
            description=(
                "Check whether one specific household member is free "
                "or busy in a given time window. Hits Google freebusy "
                "against BOTH the person's personal calendar "
                "(email_address) AND their work calendar (work_email) "
                "when both are configured, and merges the results so "
                "a slot only counts as free if the person is free on "
                "both. Returns per_calendar so you can mention if a "
                "specific calendar isn't shared with the assistant. "
                "Free/busy contains NO event detail (titles, "
                "locations) so this tool is safe to call for any "
                "household member regardless of who is asking."
            ),
            parameters=_CALENDAR_CHECK_SCHEMA,
            handler=_handle_calendar_check_availability,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Is Ben free Friday afternoon?",
                "Is Mom busy at 3pm tomorrow?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_find_free_slots",
            label="Find a free time for someone",
            description=(
                "Suggest open time slots for one household member "
                "across a window (up to ~1 month). Considers BOTH "
                "personal and work calendars when configured — a "
                "suggested slot is only free if the person is free "
                "on every shared calendar. Defaults to 30-minute "
                "slots inside 9am-6pm working hours, configurable. "
                "Warns if any calendar exists on the profile but "
                "isn't shared with the assistant."
            ),
            parameters=_CALENDAR_FREE_SLOTS_SCHEMA,
            handler=_handle_calendar_find_free_slots,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Find me a time Ben is free next week.",
                "When can Sarah do a 45-minute call this Thursday?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_for_person",
            label="List events for one person",
            description=(
                "List the actual events on a household member's "
                "personal AND work calendars between two timestamps. "
                "Honours the household's calendar-detail privacy "
                "rule: only the SUBJECT and their SPOUSE see event "
                "titles / locations; everyone else (parents, "
                "children, siblings, in-laws) gets the timing as "
                "free/busy with the title replaced by '[busy — "
                "private]'. Use when the user wants 'what's on "
                "Ben's schedule this week?' style detail. For 'is X "
                "free?' use calendar_check_availability instead — "
                "it's cheaper and doesn't leak detail."
            ),
            parameters=_CALENDAR_LIST_FOR_PERSON_SCHEMA,
            handler=_handle_calendar_list_for_person,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on Ben's calendar this week?",
                "Show me Sarah's meetings tomorrow.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_create",
            label="Create a household task",
            description=(
                "Add a new task to the family kanban board. Use when "
                "the user says things like 'add a task to…', 'remind "
                "me to…', 'we should…', or describes work they want "
                "tracked. The speaker is recorded as the creator "
                "automatically; if no assignee is supplied the "
                "speaker also becomes the owner. Default priority is "
                "'normal'; bump to 'urgent'/'high' only when the user "
                "is explicit, and use 'future_idea' for casual "
                "'someday' mentions so they don't pollute the active "
                "board. After creating a task, briefly confirm what "
                "was tracked (title + priority + owner) in 1-2 "
                "sentences."
            ),
            parameters=_TASK_CREATE_SCHEMA,
            handler=_handle_task_create,
            timeout_seconds=5.0,
            examples=(
                "Add a task to fix the east gate latch this weekend.",
                "Remind me to renew Maddie's passport — high priority.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_list",
            label="List household tasks",
            description=(
                "List tasks on the family kanban with filters. Use "
                "mine_only=true for 'my tasks' / 'what's on my plate', "
                "priority='urgent'|'high' for 'what's urgent for me?', "
                "and q='passport' for free-text search. Excludes done "
                "tasks by default — pass include_done=true when the "
                "user is asking what they finished. Returns a compact "
                "list (no descriptions) — call task_get for full "
                "detail on a specific row."
            ),
            parameters=_TASK_LIST_SCHEMA,
            handler=_handle_task_list,
            timeout_seconds=5.0,
            examples=(
                "What are my high priority tasks?",
                "What's on the family task board right now?",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_get",
            label="Get full task detail",
            description=(
                "Read one task's full detail — description, comments, "
                "follower list, attachment count. Use after task_list "
                "when the user wants the specifics of a particular "
                "task."
            ),
            parameters=_TASK_GET_SCHEMA,
            handler=_handle_task_get,
            timeout_seconds=4.0,
            examples=(
                "Tell me more about the gate task.",
                "What's the status of the passport renewal?",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_update",
            label="Update a task",
            description=(
                "Patch one task — change status (kanban column), "
                "priority, owner, dates, title, or description. Only "
                "include the fields you want changed. Setting "
                "status='done' auto-stamps completed_at; setting it "
                "back to anything else clears it."
            ),
            parameters=_TASK_UPDATE_SCHEMA,
            handler=_handle_task_update,
            timeout_seconds=5.0,
            examples=(
                "Mark the gate task as done.",
                "Bump the passport task to urgent.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_add_comment",
            label="Comment on a task",
            description=(
                "Append a comment to a task. Defaults to "
                "author_kind='assistant' so Avi-authored notes "
                "(status changes, summaries) are clearly attributed. "
                "Use author_kind='person' when relaying a message "
                "dictated by the speaker."
            ),
            parameters=_TASK_ADD_COMMENT_SCHEMA,
            handler=_handle_task_add_comment,
            timeout_seconds=4.0,
            examples=(
                "Add a note that I picked up the parts.",
                "Comment on the passport task: appointment booked for Tuesday.",
            ),
        )
    )
    reg.register(
        Tool(
            name="telegram_invite",
            label="Invite a household member to chat on Telegram",
            description=(
                "Send a household member a one-time deep-link that "
                "opens the assistant's Telegram bot and binds their "
                "Telegram account to their Person row. Use when the "
                "user asks 'invite Sarah to Telegram', 'send Mom the "
                "Telegram link', or similar. The link is delivered "
                "by SMS when the invitee has a mobile phone on file "
                "(preferred) and falls back to email otherwise — "
                "pass channel='sms' or 'email' to force one. "
                "Telegram's rules forbid the bot from initiating a "
                "conversation, so this deep-link flow is the ONLY "
                "way to onboard someone; do not promise the user "
                "you'll 'just message them' on Telegram. Idempotent: "
                "re-asking inside the 30-day window resends the same "
                "outstanding link rather than minting a new one. "
                "After sending, briefly confirm the channel and "
                "destination so the user knows where to look ("
                "'Texted the link to Sarah at +1…' / 'Emailed it to "
                "mom@example.com')."
            ),
            parameters=_TELEGRAM_INVITE_SCHEMA,
            handler=_handle_telegram_invite,
            timeout_seconds=20.0,
            examples=(
                "Invite Sarah to chat with you on Telegram.",
                "Send Mom the Telegram link by email.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_add_follower",
            label="Add a follower to a task",
            description=(
                "Loop another household member into a task as a "
                "follower. Idempotent — returns already_following=true "
                "if the person was already attached. Use lookup_person "
                "first if you only have a name."
            ),
            parameters=_TASK_ADD_FOLLOWER_SCHEMA,
            handler=_handle_task_add_follower,
            timeout_seconds=4.0,
            examples=(
                "Loop Sarah in on the passport task.",
                "Add Ben as a follower of the gate task.",
            ),
        )
    )
    return reg


def describe_capabilities(
    registry: ToolRegistry, available: set[str]
) -> str:
    """Render the registry as a friendly bullet list for the system prompt.

    The model uses this to answer "what can you do?" / "help" with
    accurate, up-to-date answers instead of making them up. Tools
    whose capabilities aren't satisfied (e.g. Google not connected)
    are silently omitted so the model never offers things it can't
    actually do this turn.
    """
    tools_available = registry.for_capabilities(available)
    if not tools_available:
        return ""
    lines: List[str] = ["You currently have these tools:"]
    for t in tools_available:
        lines.append(f"- {t.display_label()} ({t.name}) — {t.description}")
        for ex in t.examples[:2]:
            lines.append(f'    e.g. "{ex}"')
    lines.append("")
    lines.append(
        "When the user asks 'what can you do?', 'help', or similar, "
        "summarise these capabilities in 2-4 friendly sentences. Quote "
        "ONE concrete example per capability so they know how to ask. "
        "Do not promise capabilities that aren't in the list above."
    )
    return "\n".join(lines)


def detect_capabilities(db: Session, assistant_id: Optional[int]) -> set[str]:
    """Inspect the database to figure out which capabilities are live.

    Today the only feature-flag-style capability is 'google' (= the
    assistant has connected an OAuth-authorised Google account with at
    least one usable scope). Add more here as we wire integrations.
    """
    caps: set[str] = set()
    if assistant_id is not None:
        row = google_oauth.load_credentials_row(db, assistant_id)
        if row is not None:
            scopes = set((row.scopes or "").split())
            if (
                any(s.endswith("/gmail.send") for s in scopes)
                or any(s.endswith("/gmail.modify") for s in scopes)
                or any(s.endswith("/calendar.readonly") for s in scopes)
            ):
                caps.add("google")
    return caps


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "describe_capabilities",
    "detect_capabilities",
]
