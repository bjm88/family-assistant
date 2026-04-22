"""``sql_query`` and ``lookup_person`` — read-only DB access tools.

* ``sql_query`` lets the LLM run an ad-hoc SELECT (sanitised by
  :mod:`api.ai.sql_tool`) when none of the prebuilt tools fit.
* ``lookup_person`` is a fast, privacy-aware fuzzy name lookup that
  saves the model from having to write SQL for the trivial 'who is
  Sarah?' case.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .... import models
from ... import authz, sql_tool
from .._registry import ToolContext, ToolError


# ---- sql.query --------------------------------------------------------


SQL_QUERY_SCHEMA: Dict[str, Any] = {
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


async def handle_sql_query(ctx: ToolContext, sql: str) -> Dict[str, Any]:
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


LOOKUP_PERSON_SCHEMA: Dict[str, Any] = {
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


async def handle_lookup_person(ctx: ToolContext, name: str) -> List[Dict[str, Any]]:
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
                    "jobs": [
                        {
                            "company_name": j.company_name,
                            "company_website": j.company_website,
                            "role_title": j.role_title,
                            "work_email": j.work_email,
                            "description": j.description,
                        }
                        for j in (p.jobs or [])
                    ],
                    "gender": p.gender,
                }
            )
            if len(matches) >= 5:
                break
    return matches
