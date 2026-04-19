"""Tool registry the AI agent loop can dispatch into.

A *tool* here is a small, well-typed Python function the LLM can call
to read data or take an action. We deliberately keep the registry tiny
and explicit (no class auto-discovery) so it's obvious in code review
exactly what Avi is allowed to do.

Each tool declares:

* ``name`` — what the model uses in ``tool_calls``.
* ``description`` — short natural-language hint used by the model.
* ``parameters`` — JSON Schema for the arguments. We also use it to
  validate inputs before running the handler so a hallucinated
  argument shape can't crash the executor.
* ``handler`` — the actual Python callable. Receives a
  :class:`ToolContext` as its first argument and the model-supplied
  arguments as keyword args.
* ``timeout_seconds`` — hard deadline. Anything that runs longer is
  cancelled and the tool result becomes an error.
* ``requires`` — capability flags the tool needs (e.g. "google"). The
  agent can refuse to advertise a tool when its capability is missing
  rather than letting the model call it and get a confusing error.

Adding a new tool: define a handler, wrap it in :class:`Tool`, and
register it in :func:`build_default_registry`. That's it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..integrations import google_oauth
from ..integrations.gmail import GmailSendError, send_email
from ..integrations.google_calendar import (
    CalendarError,
    list_upcoming_events,
)
from . import sql_tool


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    db: Session
    family_id: int
    assistant_id: Optional[int] = None
    person_id: Optional[int] = None  # who is talking, when known


@dataclass
class ToolResult:
    """Structured outcome we feed back to the model after each call."""

    ok: bool
    output: Any = None
    error: Optional[str] = None
    duration_ms: int = 0
    # Optional human-friendly summary for the UI step card. The full
    # ``output`` JSON also goes to the audit row; this is just the
    # one-line "Sent message gAAA…=" displayed inline.
    summary: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """Trim ``output`` for the model — keep it short to save tokens."""
        if self.ok:
            return {"ok": True, "output": _truncate_for_model(self.output)}
        return {"ok": False, "error": self.error}


def _truncate_for_model(value: Any, *, limit: int = 4000) -> Any:
    """Stringify + cap large outputs so we don't blow the context window."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > limit:
            return value[: limit - 12] + "…(truncated)"
        return value
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 12] + "…(truncated)"
    return text


ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)
    handler: ToolHandler
    timeout_seconds: float = 15.0
    requires: tuple[str, ...] = field(default_factory=tuple)
    # Human-friendly title for capability descriptions ("Send email"
    # rather than "gmail_send"). Falls back to ``name`` when omitted.
    label: Optional[str] = None
    # Sample user phrasings the LLM can quote back when asked "what
    # can you do?". Two-to-three short examples per tool keeps the
    # system prompt small while still being concrete.
    examples: tuple[str, ...] = field(default_factory=tuple)

    def display_label(self) -> str:
        return self.label or self.name

    def to_ollama_schema(self) -> Dict[str, Any]:
        """Render the tool in the Ollama / OpenAI ``tools`` shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Capability-aware collection of :class:`Tool` instances."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def for_capabilities(self, available: set[str]) -> List[Tool]:
        """Return only tools whose ``requires`` are satisfied."""
        out: List[Tool] = []
        for t in self._tools.values():
            if all(req in available for req in t.requires):
                out.append(t)
        return out

    def to_ollama_tools(self, available: set[str]) -> List[Dict[str, Any]]:
        return [t.to_ollama_schema() for t in self.for_capabilities(available)]

    async def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Run a tool by name with timeout + structured error capture."""
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"Unknown tool {name!r}. Available: {sorted(self._tools)}",
            )
        # Light validation — mostly a "did the model send a dict?" check.
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} expects an arguments object, got {type(arguments).__name__}",
            )
        # Validate required fields.
        required = tool.parameters.get("required", [])
        missing = [k for k in required if k not in arguments]
        if missing:
            return ToolResult(
                ok=False,
                error=f"Tool {name!r} missing required argument(s): {missing}",
            )

        started = time.monotonic()
        try:
            result_value = await asyncio.wait_for(
                tool.handler(ctx, **arguments),
                timeout=tool.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                error=(
                    f"Tool {name!r} timed out after {tool.timeout_seconds:.0f}s"
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ToolError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - bubble as structured error
            logger.exception("Tool %r crashed", name)
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result_value, ToolResult):
            result_value.duration_ms = result_value.duration_ms or duration_ms
            return result_value
        return ToolResult(ok=True, output=result_value, duration_ms=duration_ms)


class ToolError(RuntimeError):
    """Raised inside a tool handler to surface a clean, user-facing error
    to the agent loop without dumping a traceback."""


# ---------------------------------------------------------------------------
# Concrete tools
# ---------------------------------------------------------------------------


# ---- sql.query --------------------------------------------------------


_SQL_QUERY_SCHEMA: Dict[str, Any] = {
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


async def _handle_sql_query(ctx: ToolContext, sql: str) -> Dict[str, Any]:
    try:
        result = sql_tool.run_safe_query(
            ctx.db, sql, family_id=ctx.family_id, max_rows=50
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


_LOOKUP_PERSON_SCHEMA: Dict[str, Any] = {
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


async def _handle_lookup_person(ctx: ToolContext, name: str) -> List[Dict[str, Any]]:
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
                    "gender": p.gender,
                }
            )
            if len(matches) >= 5:
                break
    return matches


# ---- gmail.send -------------------------------------------------------


_GMAIL_SEND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient email address (one).",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": (
                "Plain-text email body. Sign off naturally as the assistant; "
                "do not include the recipient's name in the signature."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}


async def _handle_gmail_send(
    ctx: ToolContext, to: str, subject: str, body: str
) -> ToolResult:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — connect one in the admin UI."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    try:
        message_id = await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as e:
        raise ToolError(str(e)) from e

    return ToolResult(
        ok=True,
        output={"message_id": message_id, "to": to, "subject": subject},
        summary=f"Sent “{subject}” to {to}",
    )


# ---- calendar.list_upcoming -------------------------------------------


_CALENDAR_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "hours_ahead": {
            "type": "integer",
            "description": "How many hours into the future to scan. Default 72.",
            "minimum": 1,
            "maximum": 720,
        },
        "max_results": {
            "type": "integer",
            "description": "Max events to return across all calendars. Default 15.",
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": [],
}


async def _handle_calendar_list(
    ctx: ToolContext,
    hours_ahead: int = 72,
    max_results: int = 15,
) -> List[Dict[str, Any]]:
    if ctx.assistant_id is None:
        raise ToolError("No assistant is configured for this family.")
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e
    try:
        events = await asyncio.to_thread(
            list_upcoming_events,
            creds,
            hours_ahead=hours_ahead,
            max_results=max_results,
        )
    except CalendarError as e:
        raise ToolError(str(e)) from e
    return [
        {
            "summary": e.summary,
            "start": e.start,
            "end": e.end,
            "location": e.location,
            "calendar_id": e.calendar_id,
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_default_registry() -> ToolRegistry:
    """Construct the registry the chat agent uses by default."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="sql_query",
            label="Query the family database",
            description=(
                "Run a single read-only SELECT against the family database. "
                "Useful for ad-hoc lookups (vehicles, residences, insurance, "
                "etc.) when the prebuilt tools below don't fit. Always include "
                "family_id in the WHERE clause."
            ),
            parameters=_SQL_QUERY_SCHEMA,
            handler=_handle_sql_query,
            timeout_seconds=8.0,
            examples=(
                "How many cars do we own?",
                "When does our auto insurance renew?",
                "Who in the family takes blood pressure medication?",
            ),
        )
    )
    reg.register(
        Tool(
            name="lookup_person",
            label="Look up a family member",
            description=(
                "Find a household member by partial name. Returns person_id, "
                "names, email, and gender. Use this BEFORE drafting an email "
                "to a family member so you have their real address."
            ),
            parameters=_LOOKUP_PERSON_SCHEMA,
            handler=_handle_lookup_person,
            timeout_seconds=4.0,
            examples=(
                "What's Sarah's email address?",
                "Tell me about Ben.",
            ),
        )
    )
    reg.register(
        Tool(
            name="gmail_send",
            label="Send an email",
            description=(
                "Send a plain-text email from the assistant's connected "
                "Gmail account. Returns the Gmail message_id on success. "
                "ONLY call this once you have the recipient's real email "
                "address (use lookup_person first if needed) and a fully-"
                "drafted subject and body."
            ),
            parameters=_GMAIL_SEND_SCHEMA,
            handler=_handle_gmail_send,
            timeout_seconds=20.0,
            requires=("google",),
            examples=(
                "Send Mom a note thanking her for dinner.",
                "Email Ben a one-line summary of tomorrow's calendar.",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_upcoming",
            label="Read the calendar",
            description=(
                "List events on the assistant's connected Google calendar "
                "(and any calendars shared with it) for the next N hours. "
                "Use to answer 'what's coming up' or to gather context for "
                "an email about a future event."
            ),
            parameters=_CALENDAR_LIST_SCHEMA,
            handler=_handle_calendar_list,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on the calendar this week?",
                "Are we free Saturday afternoon?",
            ),
        )
    )
    return reg


def describe_capabilities(
    registry: ToolRegistry, available: set[str]
) -> str:
    """Render the registry as a friendly bullet list for the system prompt.

    The model uses this to answer "what can you do?" / "help" with
    accurate, up-to-date answers instead of making them up. Tools
    whose capabilities aren't satisfied (e.g. Google not connected)
    are silently omitted so the model never offers things it can't
    actually do this turn.
    """
    tools_available = registry.for_capabilities(available)
    if not tools_available:
        return ""
    lines: List[str] = ["You currently have these tools:"]
    for t in tools_available:
        lines.append(f"- {t.display_label()} ({t.name}) — {t.description}")
        for ex in t.examples[:2]:
            lines.append(f'    e.g. "{ex}"')
    lines.append("")
    lines.append(
        "When the user asks 'what can you do?', 'help', or similar, "
        "summarise these capabilities in 2-4 friendly sentences. Quote "
        "ONE concrete example per capability so they know how to ask. "
        "Do not promise capabilities that aren't in the list above."
    )
    return "\n".join(lines)


def detect_capabilities(db: Session, assistant_id: Optional[int]) -> set[str]:
    """Inspect the database to figure out which capabilities are live.

    Today the only feature-flag-style capability is 'google' (= the
    assistant has connected an OAuth-authorised Google account with at
    least one usable scope). Add more here as we wire integrations.
    """
    caps: set[str] = set()
    if assistant_id is not None:
        row = google_oauth.load_credentials_row(db, assistant_id)
        if row is not None:
            scopes = set((row.scopes or "").split())
            if any(s.endswith("/gmail.send") for s in scopes) or any(
                s.endswith("/calendar.readonly") for s in scopes
            ):
                caps.add("google")
    return caps


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "describe_capabilities",
    "detect_capabilities",
]
