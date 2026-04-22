"""Make ``uq_live_sessions_external_thread`` per-source.

Background
----------
The original partial unique index added in 0009_live_sessions enforced
``(family_id, external_thread_id) UNIQUE WHERE external_thread_id IS NOT NULL``.
That worked while the only sources with non-NULL thread ids were
email (Gmail thread id), SMS (E.164 phone), and Telegram (numeric chat
id) — three disjoint namespaces.

WhatsApp via Twilio (added in 0027) reuses the **phone number** as the
thread id, which collides with SMS for any household member who texts
us on both surfaces:

    sqlalchemy.exc.IntegrityError:
      duplicate key value violates unique constraint
      "uq_live_sessions_external_thread"
      Key (family_id, external_thread_id)=(2, +12039198800) already exists.

The correct invariant is **one open thread per surface per family**,
not one open thread per family across all surfaces. So we widen the
unique key to ``(family_id, source, external_thread_id)``.

Migration steps
---------------
1. Drop the old ``uq_live_sessions_external_thread`` partial unique index.
2. Recreate it with ``source`` included in the key, same partial WHERE
   clause so live (camera/chat) sessions with NULL thread ids still
   don't collide on shared NULLs.

Downgrade reverses (drops the per-source index, recreates the original
two-column one). Will fail if the resulting two-column index would
violate uniqueness — which can happen as soon as the same phone is
used on both SMS and WhatsApp; the error message tells the operator
to clean up the cross-surface duplicates first.

Revision ID: 0028_live_sess_thread_per_src
Revises: 0027_whatsapp_channel
Create Date: 2026-04-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0028_live_sess_thread_per_src"
down_revision: Union[str, None] = "0027_whatsapp_channel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "uq_live_sessions_external_thread",
        table_name="live_sessions",
    )
    op.create_index(
        "uq_live_sessions_external_thread",
        "live_sessions",
        ["family_id", "source", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_live_sessions_external_thread",
        table_name="live_sessions",
    )
    op.create_index(
        "uq_live_sessions_external_thread",
        "live_sessions",
        ["family_id", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )
