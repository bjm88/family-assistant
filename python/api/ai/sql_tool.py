"""Sandboxed read-only SQL execution for the AI assistant.

The AI assistant occasionally needs to look up data the static RAG block
doesn't include (e.g. "what's the VIN-last-four of mom's truck?"). We
let Gemma generate a narrow ``SELECT`` and execute it through this
helper, which enforces a strict allow-list:

* one statement only — no ``;`` mid-query, no ``;`` followed by more SQL
* must start with ``SELECT`` or ``WITH`` (CTE → SELECT)
* must reference only tables in :data:`schema_catalog.ALLOWED_TABLES`
* every query is wrapped in ``SELECT * FROM (<query>) sub LIMIT N`` so a
  runaway query can never return more than ``max_rows`` rows
* a per-statement ``statement_timeout`` is set so a slow plan can't
  starve the chat path

The output is a list of plain dicts (column → JSON-safe value) suitable
for handing back to the LLM as context or returning over the API.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .authz import (
    OWNER_COLUMN_BY_TABLE,
    REDACTED_PLACEHOLDER,
    SENSITIVE_COLUMNS_BY_TABLE,
    SENSITIVE_TABLES,
)
from .schema_catalog import ALLOWED_TABLES

logger = logging.getLogger(__name__)


class SqlToolError(ValueError):
    """Raised when a query fails validation or execution."""


# Anything DML/DDL/transaction-control is blocked. We're paranoid here
# because the LLM controls the SQL string.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"copy|comment|vacuum|analyze|reindex|cluster|lock|begin|commit|"
    r"rollback|savepoint|set\s+role|set\s+session|reset|listen|notify|"
    r"do\b|call\b|prepare|deallocate|execute|refresh|load|security"
    r")\b",
    re.IGNORECASE,
)

_TABLE_REF = re.compile(
    r"(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE
)

_LEADING_COMMENT = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/)", re.DOTALL)


@dataclass(frozen=True)
class SqlToolResult:
    sql: str
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sql": self.sql,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
        }


def run_safe_query(
    db: Session,
    raw_sql: str,
    *,
    family_id: int,
    max_rows: int = 200,
    timeout_ms: int = 5_000,
    accessible_subject_ids: Optional[Iterable[int]] = None,
) -> SqlToolResult:
    """Execute ``raw_sql`` after validating it is a read-only SELECT.

    ``family_id`` is provided for callers to embed in their query as a
    ``WHERE family_id = :family_id`` predicate (and for several future
    safety checks). It is *not* auto-injected because the LLM may want
    to JOIN or filter via a different column (``person_id``, etc.); we
    leave it to the prompting layer to instruct the LLM to scope.

    ``accessible_subject_ids`` is the set of person ids the speaker is
    relationship-allowed to read sensitive data for (their own id, plus
    spouses and direct children — see :mod:`ai.authz`). When supplied,
    column-level redaction kicks in: for example ``people.notes`` is
    swapped for the redacted placeholder on any row whose ``person_id``
    isn't in that set. ``None`` (legacy callers) keeps the original
    "trusted reader" behaviour.

    Fully-sensitive tables (``sensitive_identifiers``,
    ``identity_documents``, ``financial_accounts``,
    ``medical_conditions``, ``medications``, ``physicians``) are
    rejected by the validator entirely — the model is steered to the
    purpose-built tools (``reveal_sensitive_identifier`` etc.) that run
    relationship checks before returning anything.
    """
    sql, referenced_tables = _strip_and_validate(raw_sql)

    wrapped = (
        f"SELECT * FROM ({sql}) AS sandbox_sub LIMIT :__sandbox_limit"
    )

    try:
        # Each query runs in its own transaction so the SET LOCAL +
        # query are isolated and the timeout doesn't leak into the
        # caller's session.
        with db.begin_nested() if db.in_transaction() else db.begin():
            db.execute(
                text("SET LOCAL statement_timeout = :ms"),
                {"ms": int(timeout_ms)},
            )
            result = db.execute(
                text(wrapped),
                {"__sandbox_limit": int(max_rows) + 1, "family_id": family_id},
            )
            rows = result.mappings().all()
    except SQLAlchemyError as e:
        # Surface the underlying Postgres message — that's exactly what
        # the LLM needs to fix its next attempt.
        raise SqlToolError(_short_pg_error(e)) from e

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    columns = list(rows[0].keys()) if rows else []

    json_rows = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    if accessible_subject_ids is not None:
        json_rows = _redact_rows_for_speaker(
            json_rows,
            referenced_tables=referenced_tables,
            accessible_subject_ids=frozenset(
                int(x) for x in accessible_subject_ids if x is not None
            ),
        )
    return SqlToolResult(
        sql=sql,
        columns=columns,
        rows=json_rows,
        row_count=len(json_rows),
        truncated=truncated,
    )


# ---------- internals ----------------------------------------------------


def _strip_and_validate(raw_sql: str) -> tuple[str, frozenset[str]]:
    """Validate ``raw_sql`` and return ``(clean_sql, referenced_tables)``.

    The set of referenced tables is used by :func:`run_safe_query` for
    column-level redaction so we don't have to re-parse the SQL.
    """
    if not raw_sql or not raw_sql.strip():
        raise SqlToolError("Empty query.")

    sql = raw_sql.strip()
    while True:
        m = _LEADING_COMMENT.match(sql)
        if not m:
            break
        sql = sql[m.end():].lstrip()

    if sql.endswith(";"):
        sql = sql[:-1].rstrip()

    # No second statement allowed — string-level check is sufficient
    # because we already trimmed the trailing semicolon.
    if ";" in sql:
        raise SqlToolError("Only a single statement is allowed (no ';').")

    head = sql.lstrip().split(None, 1)[0].lower()
    if head not in {"select", "with"}:
        raise SqlToolError("Only SELECT / WITH queries are allowed.")

    if _FORBIDDEN_KEYWORDS.search(sql):
        raise SqlToolError("Query references a forbidden keyword.")

    referenced = {m.group(1).lower() for m in _TABLE_REF.finditer(sql)}
    bad = sorted(t for t in referenced if t not in ALLOWED_TABLES)
    if bad:
        raise SqlToolError(
            "Query references tables outside the allow-list: " + ", ".join(bad)
        )

    # Relationship-gated tables can't be queried via ad-hoc SQL — even
    # by a privileged speaker. The agent must use the dedicated tools
    # (``reveal_sensitive_identifier`` etc.) that perform per-subject
    # authorization. This is defence-in-depth: the row sanitiser below
    # would also redact them, but rejecting at the parse layer means a
    # confused model can't even *learn* whether the table is empty by
    # observing latency.
    blocked = sorted(t for t in referenced if t in SENSITIVE_TABLES)
    if blocked:
        raise SqlToolError(
            "These tables can't be queried via sql_query for privacy "
            "reasons: "
            + ", ".join(blocked)
            + ". Use the dedicated tools (e.g. reveal_sensitive_identifier "
            "for SSN-style identifiers) which run a relationship check "
            "against the speaker before returning anything."
        )

    return sql, frozenset(referenced)


def _redact_rows_for_speaker(
    rows: List[Dict[str, Any]],
    *,
    referenced_tables: frozenset[str],
    accessible_subject_ids: frozenset[int],
) -> List[Dict[str, Any]]:
    """Apply column-level redaction to a SELECT result.

    Currently only ``people.notes`` is partially-sensitive (other
    person-scoped tables are blocked outright in ``_strip_and_validate``
    above). The implementation generalises so adding a new entry to
    ``SENSITIVE_COLUMNS_BY_TABLE`` is enough to cover it.
    """
    sensitive_cols: set[str] = set()
    for table in referenced_tables:
        sensitive_cols |= SENSITIVE_COLUMNS_BY_TABLE.get(table, frozenset())
    if not sensitive_cols:
        return rows

    owner_candidates = {
        OWNER_COLUMN_BY_TABLE[t]
        for t in referenced_tables
        if t in OWNER_COLUMN_BY_TABLE
    }

    out: List[Dict[str, Any]] = []
    for row in rows:
        owner_id: Optional[int] = None
        for cand in owner_candidates:
            v = row.get(cand)
            if v is not None:
                try:
                    owner_id = int(v)
                except (TypeError, ValueError):
                    owner_id = None
                break
        if owner_id is not None and owner_id in accessible_subject_ids:
            out.append(dict(row))
            continue
        # If we could not determine an owner OR the speaker is not
        # allowed, we redact the sensitive columns. Erring on the side
        # of "redact when in doubt" matches the rule the safety
        # sandbox advertises to the model.
        red = {}
        for k, v in row.items():
            red[k] = REDACTED_PLACEHOLDER if k in sensitive_cols else v
        out.append(red)
    return out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _short_pg_error(e: SQLAlchemyError) -> str:
    msg = str(getattr(e, "orig", e)) or str(e)
    msg = msg.split("\n", 1)[0]
    return msg[:280]


__all__ = ["SqlToolError", "SqlToolResult", "run_safe_query"]
