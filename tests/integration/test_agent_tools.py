"""Smoke tests for the agent's key tool handlers.

These tests prove the **tool layer** the LLM calls into actually works
end-to-end against the test database. We bypass the LLM entirely (no
Ollama, no Gemini) and call each tool handler directly with a real
:class:`api.ai.tools.ToolContext`. That's faster, more deterministic,
and exactly what we care about: that ``_handle_task_create`` actually
inserts a tasks row, that ``_handle_web_search`` returns a clean payload
shape, etc.

Coverage matches the user's stated key features:
* Task creation (``task_kind='todo'``).
* Monitoring task creation with cron (``task_kind='monitoring'``).
* Web search (mocked at the provider boundary).

Reply-back (e.g. ``send_sms`` / ``gmail.send_reply``) is exercised by
``test_inbound_channels.py`` rather than here, because it's a
post-agent step driven by the inbound services, not a tool the LLM
calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from api import models
from api.ai import tools
from api.ai.tools import ToolContext


@pytest.fixture
def ctx(db, test_family) -> ToolContext:
    """A fresh ToolContext bound to the persistent test family + person."""
    return ToolContext(
        db=db,
        family_id=test_family["family_id"],
        assistant_id=test_family["assistant_id"],
        person_id=test_family["person_id"],
    )


# ---------------------------------------------------------------------------
# task_create — owner_kind='human', task_kind='todo'
# ---------------------------------------------------------------------------


async def test_task_create_inserts_todo_for_speaker(ctx, db, test_family):
    """A bare-minimum todo creation lands in the tasks table for the speaker."""
    result = await tools._handle_task_create(
        ctx,
        title="Integration-test todo: pick up groceries",
        priority="normal",
    )

    task_id = result["created"]["task_id"]
    row = db.get(models.Task, task_id)
    try:
        assert row is not None
        assert row.title == "Integration-test todo: pick up groceries"
        assert row.task_kind == "todo"
        assert row.owner_kind == "human"
        assert row.assigned_to_person_id == test_family["person_id"], (
            "When no explicit assignee is passed and ctx.person_id is set, "
            "the speaker should become the implicit owner."
        )
        assert row.family_id == test_family["family_id"]
        assert row.cron_schedule is None  # todos must not carry a cron
        assert row.next_run_at is None
    finally:
        db.delete(row)
        db.commit()


# ---------------------------------------------------------------------------
# task_create — owner_kind='ai', task_kind='monitoring' with cron
# ---------------------------------------------------------------------------


async def test_monitoring_task_create_parses_cron_and_sets_next_run(
    ctx, db, test_family,
):
    """An AI-owned monitoring task gets its cron parsed + next_run computed.

    The non-paused AI-monitoring path normally kicks off an immediate
    first run via the monitoring scheduler. That would call straight
    out to Ollama (which isn't running in tests) and leak a background
    thread past the test, so we patch the kickoff to a no-op. The
    cron parse + next_run_at calculation happen inside ``_handle_task_create``
    BEFORE the kickoff, so this still verifies the full create path.
    """
    with patch(
        "api.services.monitoring_scheduler.run_now_in_background"
    ) as kickoff_mock:
        result = await tools._handle_task_create(
            ctx,
            title="Integration-test monitor: weekly weather check",
            owner_kind="ai",
            task_kind="monitoring",
            cron_schedule="0 8 * * 1",  # 08:00 every Monday
        )

    task_id = result["created"]["task_id"]
    db.expire_all()  # task_create commits in its own transaction
    row = db.get(models.Task, task_id)
    try:
        assert row is not None
        assert row.task_kind == "monitoring"
        assert row.owner_kind == "ai"
        assert row.assigned_to_person_id is None, (
            "AI-owned monitoring tasks must have NO human assignee."
        )
        assert row.cron_schedule, (
            "cron_schedule must round-trip into the row when supplied "
            "for a monitoring task"
        )
        assert row.next_run_at is not None, (
            "Active (non-paused) monitoring tasks must have next_run_at "
            "computed from the cron expression."
        )
        kickoff_mock.assert_called_once_with(task_id)
    finally:
        if row is not None:
            db.delete(row)
            db.commit()


# ---------------------------------------------------------------------------
# web_search — provider mocked at the boundary
# ---------------------------------------------------------------------------


async def test_web_search_tool_returns_clean_payload(ctx):
    """``_handle_web_search`` shapes provider results for the model.

    Mocks at ``web_search_integration.search`` (the only thing the
    handler actually depends on) so the test stays offline + fast.
    Asserts the keys the local LLM expects to read back: ``query``,
    ``provider``, ``result_count``, ``results[]``, optional ``summary``.
    """
    from api.integrations import web_search

    fake_response = web_search.SearchResponse(
        query="best ice cream shop in brooklyn",
        provider="fake-provider",
        results=[
            web_search.SearchResult(
                title="Ample Hills",
                url="https://example.com/ample",
                snippet="Brooklyn ice cream shop, est. 2011.",
                source="fake-provider",
                extracted_content=None,
            ),
            web_search.SearchResult(
                title="Van Leeuwen",
                url="https://example.com/vanleeuwen",
                snippet="French ice cream truck → storefronts.",
                source="fake-provider",
                extracted_content=None,
            ),
        ],
        summary="Ample Hills and Van Leeuwen are top choices.",
        issued_queries=["best ice cream brooklyn"],
    )

    async def _fake_search(query: str, *, limit: Any = None):
        return fake_response

    with patch(
        "api.ai.tools.web_search_integration.search",
        side_effect=_fake_search,
    ):
        payload = await tools._handle_web_search(
            ctx, query="best ice cream shop in brooklyn"
        )

    assert payload["query"] == "best ice cream shop in brooklyn"
    assert payload["provider"] == "fake-provider"
    assert payload["result_count"] == 2
    assert {r["title"] for r in payload["results"]} == {
        "Ample Hills",
        "Van Leeuwen",
    }
    assert payload["summary"] == "Ample Hills and Van Leeuwen are top choices."
    assert payload["issued_queries"] == ["best ice cream brooklyn"]


async def test_web_search_tool_surfaces_provider_unavailable(ctx):
    """Provider errors must come back as a ToolError, not bubble up raw."""
    from api.integrations import web_search

    async def _fail(query: str, *, limit: Any = None):
        raise web_search.SearchUnavailable("FA_SEARCH_PROVIDER not set")

    with patch(
        "api.ai.tools.web_search_integration.search", side_effect=_fail,
    ):
        with pytest.raises(tools.ToolError) as excinfo:
            await tools._handle_web_search(ctx, query="anything")

    assert "FA_SEARCH_PROVIDER" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Cross-check that all three tools listed above are actually advertised
# to the model in the default registry. Catches the "tool quietly dropped
# from build_default_registry during refactor" failure mode.
# ---------------------------------------------------------------------------


def test_default_registry_advertises_key_tools():
    registry = tools.build_default_registry()
    advertised = {t.name for t in registry._tools.values()}
    for required in ("task_create", "web_search"):
        assert required in advertised, (
            f"Tool {required!r} is missing from build_default_registry; "
            f"got {sorted(advertised)}"
        )
