"""Initial schema.

Creates every table in the ORM metadata plus a SQL-side view
``llm_schema_catalog`` that a local LLM can ``SELECT * FROM`` to discover
every table and column with its human-readable comment. This is the
primary mechanism that makes dynamic SQL generation reliable.

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
