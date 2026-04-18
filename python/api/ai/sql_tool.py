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
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
) -> SqlToolResult:
    """Execute ``raw_sql`` after validating it is a read-only SELECT.

    ``family_id`` is provided for callers to embed in their query as a
    ``WHERE family_id = :family_id`` predicate (and for several future
    safety checks). It is *not* auto-injected because the LLM may want
    to JOIN or filter via a different column (``person_id``, etc.); we
    leave it to the prompting layer to instruct the LLM to scope.
    """
    sql = _strip_and_validate(raw_sql)

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
    return SqlToolResult(
        sql=sql,
        columns=columns,
        rows=json_rows,
        row_count=len(json_rows),
        truncated=truncated,
    )


# ---------- internals ----------------------------------------------------


def _strip_and_validate(raw_sql: str) -> str:
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

    return sql


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
