"""Initial schema (squashed baseline).

Creates every table in the ORM metadata plus a SQL-side view
``llm_schema_catalog`` that a local LLM can ``SELECT * FROM`` to discover
every table and column with its human-readable comment. This is the
primary mechanism that makes dynamic SQL generation reliable.

History
-------
This file is the **single squashed baseline** for the schema. The
project previously had a chain of 28 migrations (``0001`` through
``0028_live_session_thread_per_src``); they were collapsed in one go
because:

* ``0001`` always built the schema from ``Base.metadata.create_all()``
  against the *current* ORM, so ``alembic upgrade head`` from an empty
  DB would create the modern shape and then the chain would crash on
  later migrations that assumed the *historical* shape (e.g. ``0002``
  renaming ``relationship_to_head_of_household``, ``0026`` re-creating
  the ``jobs`` table). The chain was effectively replay-from-empty
  broken from day one.
* Squashing once is cheaper than repairing 27 historical revisions
  for a single-tenant system with one production DB and no shared
  staging environment.

The original revision files are preserved verbatim under
``versions/archive/`` for historical reference. They are NOT picked up
by alembic (alembic only scans the top of ``versions/``).

One-time prod stamp
-------------------
A production DB whose ``alembic_version`` row currently reads anything
in the ``0002`` … ``0028`` range MUST be stamped to this revision once
before ``alembic upgrade head`` will be a no-op:

    alembic stamp 0001_initial_schema

The on-disk schema was already at the modern shape (the chain ran
forward in actual time, file by file, before the squash); only the
metadata bookkeeping needs the stamp.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from api.db import Base
from api import models  # noqa: F401  (registers mappers)


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LLM_CATALOG_VIEW_SQL = """
CREATE OR REPLACE VIEW llm_schema_catalog AS
SELECT
    c.table_schema              AS table_schema,
    c.table_name                AS table_name,
    obj_description(
        (c.table_schema || '.' || c.table_name)::regclass, 'pg_class'
    )                           AS table_description,
    c.column_name               AS column_name,
    c.data_type                 AS column_data_type,
    c.is_nullable               AS column_is_nullable,
    col_description(
        (c.table_schema || '.' || c.table_name)::regclass,
        c.ordinal_position
    )                           AS column_description,
    c.ordinal_position          AS column_ordinal_position
FROM information_schema.columns c
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position;
"""


DROP_CATALOG_VIEW_SQL = "DROP VIEW IF EXISTS llm_schema_catalog;"


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    op.execute(LLM_CATALOG_VIEW_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(DROP_CATALOG_VIEW_SQL)
    Base.metadata.drop_all(bind=bind)
