"""Tiny lookup helpers for the per-family ``Assistant`` row.

Five callers in the codebase needed the same "give me the assistant_id
for this family, or ``None``" query; centralising it here keeps the
behaviour uniform (single query shape, single ``.limit(1)``, single
docstring) so future audit-trail work or caching can change one
function instead of chasing five.

Lives under ``api.ai`` because every caller is in the AI / inbound-
agent path; putting it elsewhere would force ``api.services.*`` to
import sibling services or invent a new shared module.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def assistant_id_for_family(
    db: Session, family_id: int
) -> Optional[int]:
    """Return the (sole) assistant row's id for ``family_id``, or None.

    The schema enforces one assistant per family via a unique index on
    ``assistants.family_id``, so a ``LIMIT 1`` SELECT is always exact.
    """
    return db.execute(
        select(models.Assistant.assistant_id)
        .where(models.Assistant.family_id == family_id)
        .limit(1)
    ).scalar_one_or_none()


__all__ = ["assistant_id_for_family"]
