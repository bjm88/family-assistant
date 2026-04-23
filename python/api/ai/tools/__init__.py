"""Tool registry the AI agent loop can dispatch into.

The original 4k-line ``ai/tools.py`` was split into a package by
domain:

* :mod:`._registry`         — framework: ``Tool``, ``ToolContext``,
                              ``ToolResult``, ``ToolError``,
                              ``ToolRegistry``.
* :mod:`._default_registry` — the ``build_default_registry`` wiring
                              + ``describe_capabilities`` /
                              ``detect_capabilities`` helpers. The
                              one place that knows about every tool.
* :mod:`.handlers.sql`      — ``sql_query`` + ``lookup_person``.
* :mod:`.handlers.secrets`  — gated decrypts (SSN, VIN, plate, …).
* :mod:`.handlers.messaging`— ``gmail_send``.
* :mod:`.handlers.calendar` — every ``calendar_*`` tool.
* :mod:`.handlers.tasks`    — every ``task_*`` tool.
* :mod:`.handlers.web`      — ``web_search``.
* :mod:`.handlers.telegram_invite` — ``telegram_invite`` onboarding.

This ``__init__`` is the public face of the package. It re-exports
the classes and the registry-builder so callers can keep writing
``from api.ai import tools`` and ``tools.build_default_registry()``
without having to know about the split.

A handful of private ``_handle_*`` symbols are also re-exposed for
the existing integration test suite that calls into individual
handlers; new code should go through :func:`build_default_registry`
and dispatch by tool name instead.
"""

from __future__ import annotations

from ...integrations import web_search as web_search_integration
from ._default_registry import (
    build_default_registry,
    describe_capabilities,
    detect_capabilities,
)
from ._registry import (
    InboundAttachmentRef,
    Tool,
    ToolContext,
    ToolError,
    ToolHandler,
    ToolRegistry,
    ToolResult,
)
from .handlers.tasks import handle_task_create as _handle_task_create
from .handlers.web import handle_web_search as _handle_web_search


__all__ = [
    "InboundAttachmentRef",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "describe_capabilities",
    "detect_capabilities",
    "web_search_integration",
]
