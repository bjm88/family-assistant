"""Web search providers used by Avi's monitoring + research tools.

The local agent has no native browsing — for "research college options
for Jackson" / "monitor for good Yankees ticket deals" / "track Tesla
stock price" jobs we need an external search API. This module exposes
a tiny ``WebSearchProvider`` interface and ships three adapters out of
the box; adding Serper / DuckDuckGo / SerpAPI later is one new file
and one new ``elif`` in :func:`get_provider`.

Why pluggable
-------------
* **Gemini** (default) — uses the Gemini API's built-in
  ``google_search`` tool. This is the highest-leverage choice for our
  setup: Gemini does the search **and** synthesises a grounded answer
  with citations in a single round trip, which means the local Gemma
  doesn't have to read 5+ raw SERP snippets to figure out what's going
  on. The synthesised answer is returned in
  :attr:`SearchResponse.summary` and the underlying URLs in
  :attr:`SearchResponse.results` so downstream code (and Avi's
  ``task_attach_link`` calls) still get clean source citations. We
  already require ``GEMINI_API_KEY`` for avatar generation, so 0 net
  new dependencies.
* **Brave Search** — classic SERP API, free tier 2k queries/month,
  good fallback if you'd rather pay Brave than Google or want to keep
  Gemini-spend down to image gen only.
* **Tavily** — purpose-built for AI agents, returns extracted page
  content alongside the SERP. Useful when you want raw page text for
  the local model to chew on.

Selection happens via :data:`Settings.FA_SEARCH_PROVIDER`; if no
provider is configured (or its API key is missing) the agent's
``web_search`` tool returns a clear "search not configured" error
instead of crashing the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Protocol

import httpx

from ..config import get_settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    """One result row, normalised across providers.

    ``source`` is the provider name so the UI can label "via Brave" or
    "via Tavily" if it ever wants to.
    """

    title: str
    url: str
    snippet: str
    source: str
    extracted_content: Optional[str] = None  # Tavily-style page extract


@dataclass
class SearchResponse:
    query: str
    results: List[SearchResult] = field(default_factory=list)
    provider: str = ""
    # Provider-supplied synthesised answer, if any. Gemini's
    # ``google_search`` grounding produces a paragraph-or-two summary
    # with inline citations; classic SERP providers (Brave) leave this
    # as ``None`` and the model has to read snippets directly.
    summary: Optional[str] = None
    # The actual queries the provider ran on our behalf. Gemini may
    # rewrite "find me cheap may yankees tickets" into one or more
    # google.com queries; surfacing them helps the local LLM (and
    # debugging) understand what was actually asked.
    issued_queries: List[str] = field(default_factory=list)


class SearchUnavailable(RuntimeError):
    """Raised when no provider is configured or its key is missing.

    Distinct from network errors so the agent tool can render a
    targeted "ask the admin to set BRAVE_SEARCH_API_KEY" message
    instead of a generic 500.
    """


class WebSearchProvider(Protocol):
    """Minimal contract every provider must satisfy."""

    name: str

    async def search(self, query: str, *, limit: int) -> SearchResponse: ...


# ---------------------------------------------------------------------------
# Gemini google_search adapter (default)
# ---------------------------------------------------------------------------


class GeminiSearchProvider:
    """Adapter for Gemini's built-in ``google_search`` tool grounding.

    One Gemini call gives us *both* a synthesised answer ("Best Yankees
    deals in May are X, Y, Z because ...") and a list of citation URLs
    pulled from real Google results. We hand the answer back as
    :attr:`SearchResponse.summary` and the citations as the
    :attr:`SearchResponse.results` list so the rest of the pipeline
    (the agent tool, Avi's ``task_attach_link`` calls, the UI's
    "Sources" section) keeps working uniformly across providers.

    Why this matters for our setup
    ------------------------------
    The local Gemma running monitoring jobs is good at synthesising
    *family-context-aware* updates — "the Yankees-Mets May 14 game is
    in your kid's spring break window, here are 3 cheap options" — but
    less good at independently judging which of 8 raw SERP snippets is
    authoritative. Letting Gemini do the *first* pass (search +
    grounded summary) and Gemma do the *second* pass (re-read with
    family context, decide what's actionable, write the comment) plays
    each model to its strengths and burns far less of Gemma's context
    window on raw SERP noise.

    Cost note: each call is one ``gemini-2.5-flash`` invocation plus
    grounding fees (currently a small per-grounded-request charge on
    paid Gemini API tiers). Free-tier keys are also eligible for a
    limited number of grounded requests per day.
    """

    name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str, model: Optional[str] = None):
        # Late-import the SDK so `import web_search` stays cheap on
        # boxes that haven't installed google-genai (this package is
        # only required when Gemini is actually selected as the
        # provider — the adapter raises on first call, not import).
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:  # pragma: no cover - depends on env
            raise SearchUnavailable(
                "google-genai is not installed; cannot use the Gemini "
                "search provider. `uv sync` (or `pip install "
                "google-genai`) and try again."
            ) from exc

        self._genai = genai
        self._types = genai_types
        self._client = genai.Client(api_key=api_key)
        self._model = model or self.DEFAULT_MODEL

    async def search(self, query: str, *, limit: int) -> SearchResponse:
        # The google-genai SDK's `generate_content` is sync. Run it on
        # a worker thread so we don't block the asyncio event loop.
        import asyncio

        def _call() -> SearchResponse:
            return self._sync_search(query, limit=limit)

        return await asyncio.to_thread(_call)

    def _sync_search(self, query: str, *, limit: int) -> SearchResponse:
        types = self._types
        # Bound the model's verbosity — we want a tight, citable
        # answer, not an essay. ~`limit` results is the right ballpark.
        prompt = (
            f"Use Google Search to research the following request, "
            f"then write a concise factual summary (3-6 sentences). "
            f"Cite specific dates, prices, places, or numbers when "
            f"the source supports them. Aim to surface roughly "
            f"{max(1, min(limit, 10))} distinct authoritative sources.\n\n"
            f"Request: {query}"
        )
        try:
            tool = types.Tool(google_search=types.GoogleSearch())
            config = types.GenerateContentConfig(
                tools=[tool],
                # Slightly cooler than chat default — we want the
                # synthesis to stick to what the citations support.
                temperature=0.2,
            )
            resp = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - normalize to our error
            raise SearchUnavailable(
                f"Gemini google_search call failed: {exc}"
            ) from exc

        summary = (getattr(resp, "text", None) or "").strip() or None

        results: List[SearchResult] = []
        issued_queries: List[str] = []
        seen_urls: set[str] = set()

        # google-genai exposes citations in
        # ``candidate.grounding_metadata.grounding_chunks``; each chunk
        # has a ``.web`` with ``.uri`` + ``.title``. The SDK's exact
        # attribute names have shifted across minor versions, so use
        # ``getattr`` with defaults rather than indexing.
        for cand in getattr(resp, "candidates", None) or []:
            gm = getattr(cand, "grounding_metadata", None)
            if gm is None:
                continue
            for q in getattr(gm, "web_search_queries", None) or []:
                if q and q not in issued_queries:
                    issued_queries.append(q)
            for chunk in getattr(gm, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                if web is None:
                    continue
                url = (getattr(web, "uri", None) or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = (getattr(web, "title", None) or url).strip()
                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        # Gemini doesn't return per-citation snippets;
                        # the synthesis IS the snippet. Leave snippet
                        # empty so the model leans on the summary +
                        # title to decide which URLs to attach.
                        snippet="",
                        source=self.name,
                    )
                )
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break

        return SearchResponse(
            query=query,
            results=results,
            provider=self.name,
            summary=summary,
            issued_queries=issued_queries,
        )


# ---------------------------------------------------------------------------
# Brave Search adapter
# ---------------------------------------------------------------------------


class BraveSearchProvider:
    """Adapter for https://api.search.brave.com.

    Free tier: 2,000 queries/month with rate limit 1 qps. The ``Subscription
    Token`` header is the API key from the Brave dashboard. We use the
    ``web/search`` endpoint and pull the ``web.results`` array.
    """

    name = "brave"
    _ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str, *, limit: int) -> SearchResponse:
        params = {
            "q": query,
            # Brave caps `count` at 20; we still bound it server-side
            # because the agent shouldn't burn a 50-result quota on
            # one tool call.
            "count": max(1, min(int(limit), 20)),
        }
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    self._ENDPOINT, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"Brave search transport error: {exc}") from exc

        if r.status_code == 401:
            raise SearchUnavailable(
                "Brave rejected the API key (401). Re-issue the token "
                "at https://api.search.brave.com and set "
                "BRAVE_SEARCH_API_KEY."
            )
        if r.status_code == 429:
            raise SearchUnavailable(
                "Brave search rate limit hit (429). Retry shortly or "
                "upgrade the subscription tier."
            )
        if r.status_code >= 400:
            raise SearchUnavailable(
                f"Brave search returned HTTP {r.status_code}: {r.text[:200]}"
            )

        try:
            payload = r.json()
        except ValueError as exc:
            raise SearchUnavailable(
                f"Brave search returned non-JSON: {exc}"
            ) from exc

        web = payload.get("web") or {}
        rows = web.get("results") or []
        results: List[SearchResult] = []
        for row in rows:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            snippet = (row.get("description") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=title or url,
                    url=url,
                    snippet=snippet,
                    source=self.name,
                )
            )

        return SearchResponse(query=query, results=results, provider=self.name)


# ---------------------------------------------------------------------------
# Tavily adapter (optional — enabled when FA_SEARCH_PROVIDER=tavily)
# ---------------------------------------------------------------------------


class TavilySearchProvider:
    """Adapter for https://api.tavily.com.

    Tavily returns extracted page content alongside the SERP, which is
    ideal for "deep research" monitoring runs — fewer round trips, no
    HTML scraping. Free tier: 1,000 searches/month.
    """

    name = "tavily"
    _ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str, *, limit: int) -> SearchResponse:
        body = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max(1, min(int(limit), 20)),
            "search_depth": "advanced",
            "include_answer": False,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(self._ENDPOINT, json=body)
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"Tavily transport error: {exc}") from exc

        if r.status_code == 401:
            raise SearchUnavailable(
                "Tavily rejected the API key (401). Re-issue at "
                "https://tavily.com and set TAVILY_API_KEY."
            )
        if r.status_code >= 400:
            raise SearchUnavailable(
                f"Tavily returned HTTP {r.status_code}: {r.text[:200]}"
            )

        try:
            payload = r.json()
        except ValueError as exc:
            raise SearchUnavailable(
                f"Tavily returned non-JSON: {exc}"
            ) from exc

        results: List[SearchResult] = []
        for row in payload.get("results") or []:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    title=(row.get("title") or url).strip(),
                    url=url,
                    snippet=(row.get("content") or "")[:400],
                    source=self.name,
                    extracted_content=row.get("raw_content"),
                )
            )

        return SearchResponse(query=query, results=results, provider=self.name)


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def get_provider() -> Optional[WebSearchProvider]:
    """Build the configured provider, or ``None`` if disabled.

    Returning ``None`` lets the agent tool render a friendly
    "search not configured" message instead of raising — useful for
    fresh installs that haven't picked a search backend yet.
    """

    settings = get_settings()
    name = (settings.FA_SEARCH_PROVIDER or "").strip().lower()
    if not name:
        return None

    if name == "gemini":
        if not settings.GEMINI_API_KEY:
            logger.info(
                "web_search: FA_SEARCH_PROVIDER=gemini but "
                "GEMINI_API_KEY is unset; web_search disabled."
            )
            return None
        try:
            return GeminiSearchProvider(settings.GEMINI_API_KEY)
        except SearchUnavailable as exc:
            logger.info("web_search: %s", exc)
            return None

    if name == "brave":
        if not settings.BRAVE_SEARCH_API_KEY:
            logger.info(
                "web_search: FA_SEARCH_PROVIDER=brave but "
                "BRAVE_SEARCH_API_KEY is unset; web_search disabled."
            )
            return None
        return BraveSearchProvider(settings.BRAVE_SEARCH_API_KEY)

    if name == "tavily":
        if not settings.TAVILY_API_KEY:
            logger.info(
                "web_search: FA_SEARCH_PROVIDER=tavily but "
                "TAVILY_API_KEY is unset; web_search disabled."
            )
            return None
        return TavilySearchProvider(settings.TAVILY_API_KEY)

    logger.warning(
        "web_search: unknown FA_SEARCH_PROVIDER=%r; disabling web_search.",
        name,
    )
    return None


async def search(query: str, *, limit: Optional[int] = None) -> SearchResponse:
    """Convenience wrapper used by the agent tool + monitoring loop."""

    provider = get_provider()
    if provider is None:
        raise SearchUnavailable(
            "Web search is not configured. Set FA_SEARCH_PROVIDER and "
            "the matching API key (GEMINI_API_KEY for the default, or "
            "BRAVE_SEARCH_API_KEY / TAVILY_API_KEY for the alternatives) "
            "in .env to enable Avi's web research."
        )
    settings = get_settings()
    n = limit if limit is not None else settings.AI_WEB_SEARCH_DEFAULT_LIMIT
    return await provider.search(query, limit=n)
