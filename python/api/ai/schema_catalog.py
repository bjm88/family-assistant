"""Dump the live Postgres schema for the LLM.

The migration ``0001_initial_schema`` ships a ``llm_schema_catalog`` view
that joins ``information_schema.columns`` with the ``COMMENT ON ...``
metadata attached to every table and column. By querying that single
view we can hand the LLM a complete, always-current map of the database
without hand-maintaining a separate prompt artifact.

Two public helpers:

* :func:`fetch_catalog` returns the raw rows (handy for tests / the
  ``/sql`` debugging endpoint).
* :func:`dump_text` formats the catalog as a tight Markdown-style
  bullet list ready to splice into a system prompt. The result is
  cached per-process — the schema only changes when a migration runs,
  and reusing the same string keeps Ollama's KV cache hot across
  successive chat turns.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CatalogColumn:
    table_name: str
    table_description: Optional[str]
    column_name: str
    data_type: str
    is_nullable: bool
    column_description: Optional[str]
    ordinal: int


# Tables Avi should know exist (and be allowed to query). Anything not
# in this set is hidden from the dump so the LLM doesn't try to
# inspect Alembic's bookkeeping or our internal face_embeddings (which
# carry binary blobs that would blow up token budgets).
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "families",
        "people",
        "person_relationships",
        "person_photos",
        "goals",
        "medical_conditions",
        "medications",
        "physicians",
        "pets",
        "pet_photos",
        "residences",
        "residence_photos",
        "addresses",
        "vehicles",
        "insurance_policies",
        "insurance_policy_people",
        "insurance_policy_vehicles",
        "financial_accounts",
        "identity_documents",
        "sensitive_identifiers",
        "documents",
        "assistants",
        "live_sessions",
        "live_session_participants",
        "live_session_messages",
    }
)


# Encrypted columns are never surfaced to the LLM. Last-four helper
# columns are.
_ENCRYPTED_SUFFIX = "_encrypted"


_cache_lock = threading.Lock()
_cached_dump: Optional[str] = None


def fetch_catalog(db: Session) -> List[CatalogColumn]:
    """Pull the entire ``llm_schema_catalog`` view, filtered to the
    tables we want the LLM to see."""
    rows = db.execute(
        text(
            """
            SELECT table_name,
                   table_description,
                   column_name,
                   column_data_type,
                   column_is_nullable,
                   column_description,
                   column_ordinal_position
              FROM llm_schema_catalog
             WHERE table_schema = 'public'
             ORDER BY table_name, column_ordinal_position
            """
        )
    ).all()
    out: List[CatalogColumn] = []
    for r in rows:
        if r.table_name not in ALLOWED_TABLES:
            continue
        if r.column_name.endswith(_ENCRYPTED_SUFFIX):
            continue
        out.append(
            CatalogColumn(
                table_name=r.table_name,
                table_description=r.table_description,
                column_name=r.column_name,
                data_type=r.column_data_type,
                is_nullable=str(r.column_is_nullable).upper() == "YES",
                column_description=r.column_description,
                ordinal=int(r.column_ordinal_position),
            )
        )
    return out


def dump_text(db: Session, *, force: bool = False) -> str:
    """Render the catalog as a compact prompt-ready bullet list.

    The result is cached for the life of the process. Pass
    ``force=True`` after running a migration in dev to refresh.
    """
    global _cached_dump
    with _cache_lock:
        if _cached_dump is not None and not force:
            return _cached_dump

        cols = fetch_catalog(db)
        by_table: Dict[str, List[CatalogColumn]] = {}
        descriptions: Dict[str, Optional[str]] = {}
        for c in cols:
            by_table.setdefault(c.table_name, []).append(c)
            descriptions.setdefault(c.table_name, c.table_description)

        lines: List[str] = []
        for table in sorted(by_table.keys()):
            desc = descriptions.get(table) or ""
            lines.append(f"### {table}")
            if desc:
                lines.append(f"{_collapse(desc)}")
            for col in by_table[table]:
                bits = [f"`{col.column_name}` ({col.data_type})"]
                if not col.is_nullable:
                    bits.append("NOT NULL")
                line = "- " + " · ".join(bits)
                if col.column_description:
                    line += f" — {_collapse(col.column_description)}"
                lines.append(line)
            lines.append("")  # blank line between tables

        rendered = "\n".join(lines).rstrip()
        _cached_dump = rendered
        return rendered


def reset_cache() -> None:
    """Drop the cached schema dump (call after migrations)."""
    global _cached_dump
    with _cache_lock:
        _cached_dump = None


def _collapse(text_value: str) -> str:
    """Collapse whitespace so multi-line comments don't break the bullet."""
    return " ".join(text_value.split())


__all__ = [
    "ALLOWED_TABLES",
    "CatalogColumn",
    "dump_text",
    "fetch_catalog",
    "reset_cache",
]
