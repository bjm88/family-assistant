"""``web_search`` — real-time grounded search via the configured provider."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ....integrations import web_search as web_search_integration
from .._registry import ToolContext, ToolError


WEB_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Plain-language search query. Be specific — include "
                "the household-relevant qualifiers ('Yankees tickets "
                "May 2026 cheap', 'Tesla stock news today'). Avoid "
                "site-restricted operators; the provider handles "
                "ranking."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 15,
            "description": (
                "Max number of result rows to return. Defaults to "
                "the configured household default (typically 5). "
                "Bump up only when you genuinely need more breadth."
            ),
        },
    },
    "required": ["query"],
}


async def handle_web_search(
    ctx: ToolContext,
    query: str,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    cleaned = (query or "").strip()
    if not cleaned:
        raise ToolError("Cannot run an empty web search.")
    try:
        response = await web_search_integration.search(cleaned, limit=limit)
    except web_search_integration.SearchUnavailable as exc:
        raise ToolError(str(exc)) from exc

    payload: Dict[str, Any] = {
        "query": response.query,
        "provider": response.provider,
        "result_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                # Tavily-style page extracts can be huge; truncate so
                # one tool call doesn't blow the context window.
                "extract": (
                    (r.extracted_content[:1500] + "…")
                    if r.extracted_content and len(r.extracted_content) > 1500
                    else r.extracted_content
                ),
            }
            for r in response.results
        ],
    }
    # Gemini-style providers return a synthesised, grounded answer
    # alongside the citations. Surface it so the local LLM can read
    # one paragraph instead of guessing from raw snippets — and so
    # the model is reminded that the URLs in ``results`` are the
    # citations that back the summary (which it should pass to
    # ``task_attach_link``).
    if response.summary:
        payload["summary"] = response.summary
    if response.issued_queries:
        payload["issued_queries"] = response.issued_queries
    return payload
