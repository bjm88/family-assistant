"""Registry plumbing — the framework half of the ``ai.tools`` package.

Carved out of the original 4k-line ``ai/tools.py`` so the *concrete*
tools live in their own focused modules under ``handlers/``. Nothing
in here imports a handler — keeping this module dependency-free in
the upward direction means handlers can import the framework without
risking a cycle.

A *tool* is a small, well-typed Python function the LLM can call to
read data or take an action. Each :class:`Tool` declares:

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
* ``label`` / ``examples`` — purely cosmetic, fed into the system
  prompt's "what can you do?" section by
  :func:`describe_capabilities` (in ``_default_registry``).

Adding a new tool: define a handler in the right ``handlers/<domain>.py``
module, then register it in :mod:`api.ai.tools._default_registry`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundAttachmentRef:
    """Pointer to one file that arrived with the current inbound message.

    Each inbound surface (email, SMS / WhatsApp, Telegram, future live
    chat) builds one of these per attachment it persisted before
    handing off to the agent loop, then stuffs the list onto
    :class:`ToolContext`. Tools that need to act on "the file the user
    just sent" — currently only ``task_attach_message_attachment`` —
    look it up by ``media_index`` (the same 1-based index that already
    appears in the rendered ``[Attachment N: …]`` block in the user
    message), copy the bytes out via :attr:`stored_path`, and stop.

    ``stored_path`` is relative to ``FA_STORAGE_ROOT`` and is the
    durable copy on disk — these refs intentionally do NOT carry the
    inbox-table primary key because for email the attachment row
    isn't inserted until after the agent loop completes, and we want
    every channel to use the same handle.
    """

    media_index: int
    filename: str
    mime_type: Optional[str]
    size_bytes: Optional[int]
    stored_path: str
    # ``"email" | "sms" | "whatsapp" | "telegram" | "live"`` — purely
    # for log lines and provenance in TaskAttachment.caption. The tool
    # works the same regardless of channel.
    channel: str = "msg"


@dataclass
class ToolContext:
    """Per-call execution context handed to every tool handler."""

    db: Session
    family_id: int
    assistant_id: Optional[int] = None
    person_id: Optional[int] = None  # who is talking, when known
    # Operator override — set when the speaker is an admin (Avi
    # logged in as the assistant, ``ADMIN_EMAILS`` operators). Tools
    # that gate on the household relationship matrix
    # (``reveal_sensitive_identifier``, ``reveal_secret``, etc.)
    # treat this as a bypass: every household member is in scope and
    # the audit log records ``label='admin'``. Anonymous /
    # unidentified speakers (``person_id=None``) without admin still
    # get the same refusal they got before.
    is_admin: bool = False
    # Files attached to the inbound message that triggered this agent
    # turn. Populated by the inbound-channel services (email_inbox,
    # sms_inbox, telegram_inbox); empty list otherwise. Tools that
    # want to act on "the PDF the user just sent" read this.
    inbound_attachments: List["InboundAttachmentRef"] = field(default_factory=list)


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


class ToolError(RuntimeError):
    """Raised inside a tool handler to surface a clean, user-facing error
    to the agent loop without dumping a traceback."""


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

        arg_keys = sorted(arguments.keys())
        logger.info(
            "[tool] %s start args=[%s] timeout=%.0fs",
            name,
            ",".join(arg_keys),
            tool.timeout_seconds,
        )
        started = time.monotonic()
        try:
            result_value = await asyncio.wait_for(
                tool.handler(ctx, **arguments),
                timeout=tool.timeout_seconds,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[tool] %s timeout after %dms (limit=%.0fs)",
                name,
                duration_ms,
                tool.timeout_seconds,
            )
            return ToolResult(
                ok=False,
                error=(
                    f"Tool {name!r} timed out after {tool.timeout_seconds:.0f}s"
                ),
                duration_ms=duration_ms,
            )
        except ToolError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "[tool] %s error duration_ms=%d msg=%s",
                name,
                duration_ms,
                exc,
            )
            return ToolResult(
                ok=False,
                error=str(exc),
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 - bubble as structured error
            logger.exception("[tool] %s crashed", name)
            return ToolResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result_value, ToolResult):
            result_value.duration_ms = result_value.duration_ms or duration_ms
            logger.info(
                "[tool] %s done ok=%s duration_ms=%d summary=%s",
                name,
                result_value.ok,
                result_value.duration_ms,
                (result_value.summary or "")[:120],
            )
            return result_value
        logger.info(
            "[tool] %s done ok=True duration_ms=%d", name, duration_ms
        )
        return ToolResult(ok=True, output=result_value, duration_ms=duration_ms)


__all__ = [
    "InboundAttachmentRef",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
]
