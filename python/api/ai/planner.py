"""SQL planner used by the live chat endpoint.

The planner is an *optional* pre-step that asks the lightweight Gemma
model which read-only ``SELECT`` queries (if any) would help answer the
user's next message. The results are injected into the system prompt
under a "Live data (just queried for this turn)" section so the heavy
chat model can answer with grounded facts without having to plan + run
the queries itself.

Disabled by default (see :setting:`AI_RAG_PLANNER_ENABLED`) because the
extra round trip adds 5–10 s of latency on Apple Silicon and the static
RAG block already covers most household questions. Lives behind its own
module so the chat router stays focused on streaming + audit bookkeeping.
"""

from __future__ import annotations

import json
import logging
from typing import List

from sqlalchemy.orm import Session

from . import ollama, prompts, sql_tool


logger = logging.getLogger(__name__)


_PLANNER_INSTRUCTIONS = (
    "You are the data-fetching layer for a family AI assistant. Decide "
    "which database SELECT queries (if any) would help answer the user's "
    "next message. Use the schema documentation in the system prompt.\n\n"
    "Rules:\n"
    "* Reply with ONLY a JSON object: {\"queries\": [\"SELECT ...\", ...]}.\n"
    "* Use [] when the household context already contains the answer.\n"
    "* Each query MUST be a single SELECT or WITH ... SELECT, no semicolons.\n"
    "* Always scope by family_id = {family_id} where the table has it.\n"
    "* Prefer narrow column lists; never SELECT * on people, vehicles, "
    "  insurance_policies, or financial_accounts.\n"
    "* Do not query encrypted columns (anything ending in _encrypted) — "
    "  use the *_last_four helpers.\n"
    "* Cap at 3 queries per turn.\n"
)


async def plan_queries(
    *,
    family_id: int,
    rag_block: str,
    schema_dump: str,
    last_user_message: str,
) -> List[str]:
    """Ask the LLM which SELECTs (if any) to pre-run for this turn."""
    instructions = _PLANNER_INSTRUCTIONS.replace("{family_id}", str(family_id))
    prompt = (
        f"{instructions}\n\n"
        f"--- Household context ---\n{rag_block}\n\n"
        f"--- Schema ---\n{schema_dump}\n\n"
        f"--- User just said ---\n{last_user_message}\n\n"
        'Reply with a JSON object only, e.g. {"queries": []}.'
    )
    # The planner is a structured-output task — Gemma's tiny e2b
    # variant runs it in well under a second on Apple Silicon, so we
    # never want to burn a 26B-parameter pass on it. The fallback
    # wrapper auto-routes to the main chat model when the fast tag
    # isn't pulled (yet), so a missing model doesn't break chat.
    try:
        raw, _ = await ollama.generate_with_fallback(
            prompt,
            primary_model=ollama.fast_model(),
            system=prompts.with_safety(
                "You return strict JSON. No prose. No markdown fences."
            ),
            temperature=0.1,
            max_tokens=400,
        )
    except ollama.OllamaError as e:
        logger.warning("Planner call failed: %s", e)
        return []

    queries = parse_planner_output(raw)
    return queries[:3]


def parse_planner_output(raw: str) -> List[str]:
    """Extract a JSON queries list from the planner reply, tolerantly."""
    if not raw:
        return []
    text_value = raw.strip()
    # Strip ``` fences if the model added them despite instructions.
    if text_value.startswith("```"):
        text_value = text_value.strip("`")
        if text_value.lower().startswith("json"):
            text_value = text_value[4:]
        text_value = text_value.strip()
    # Locate the first {...} block to be resilient to leading text.
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        obj = json.loads(text_value[start : end + 1])
    except json.JSONDecodeError:
        return []
    qs = obj.get("queries") if isinstance(obj, dict) else None
    if not isinstance(qs, list):
        return []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()]


def execute_planner_queries(
    db: Session, family_id: int, queries: List[str]
) -> str:
    """Run each planner-generated query, return a prompt-ready block.

    Failures are surfaced inline so the LLM can see what didn't work and
    avoid suggesting the same query the next turn.
    """
    if not queries:
        return ""
    sections: List[str] = []
    for i, q in enumerate(queries, 1):
        header = f"### Query {i}\n```sql\n{q}\n```"
        try:
            result = sql_tool.run_safe_query(db, q, family_id=family_id)
        except sql_tool.SqlToolError as e:
            sections.append(f"{header}\nError: {e}")
            continue
        if not result.rows:
            sections.append(f"{header}\nNo rows.")
            continue
        body = json.dumps(result.rows, ensure_ascii=False, default=str)
        truncation = " (truncated)" if result.truncated else ""
        sections.append(
            f"{header}\nRows ({result.row_count}{truncation}): {body}"
        )
    return "\n\n".join(sections)


__all__ = ["plan_queries", "parse_planner_output", "execute_planner_queries"]
