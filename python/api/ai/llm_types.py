"""Types shared by every LLM backend (Grok, Claude, Ollama) and the router.

Backends speak their own wire formats but all accept and return these
shapes, so :mod:`api.ai.llm` can move a conversation from one backend to
another mid-task (e.g. Grok drops out halfway through an agent loop and
the next turn runs on the local model).

Conversation history is kept in the OpenAI Chat Completions shape:

* ``{"role": "system" | "user", "content": str}``
* ``{"role": "assistant", "content": str, "tool_calls": [...]}`` where
  each call is ``{"id", "type": "function", "function": {"name",
  "arguments": dict}}``
* ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``

Keys starting with ``_`` carry provider-private state (Claude's raw
content blocks, which must be echoed back for its thinking to survive a
tool round-trip). Other backends drop them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List


class LLMError(RuntimeError):
    """Base for anything an LLM backend raised."""


class LLMUnavailable(LLMError):
    """Backend unreachable, key missing or rejected, or overloaded.

    The router treats this as "this backend is down" and skips it for a
    cooldown window.
    """


class LLMModelMissing(LLMUnavailable):
    """The backend is up but the requested model doesn't exist there.

    Only that model is skipped; other models on the same backend are
    still tried.
    """


class LLMTimeout(LLMError):
    """Backend accepted the request but did not answer in time."""


@dataclass
class ToolCall:
    """Single tool invocation requested by the LLM."""

    name: str
    arguments: Dict[str, Any]
    id: str = ""


@dataclass
class ChatWithToolsResult:
    """Outcome of one tool-calling round-trip."""

    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    raw_message: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    # Provider-private keys to keep on the assistant history message.
    history_extras: Dict[str, Any] = field(default_factory=dict)

    def history_message(self) -> Dict[str, Any]:
        """The assistant turn to append to the conversation history."""
        msg: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        msg.update(self.history_extras)
        return msg


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def parse_arguments(raw: Any) -> Dict[str, Any]:
    """Tool-call arguments arrive as a dict or a JSON string; return a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def canonical_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy ``messages`` with every tool call id'd and paired.

    Older turns (or a local model that emits calls without ids) can leave
    an assistant ``tool_calls`` entry with no ``id`` and its ``tool``
    reply with no ``tool_call_id``. Cloud APIs reject that, so missing
    ids are minted here and unpaired tool replies are matched to the
    preceding assistant's calls in order.
    """
    out: List[Dict[str, Any]] = []
    pending: List[Dict[str, str]] = []
    for raw in messages:
        msg = dict(raw)
        role = msg.get("role") or "user"
        msg["role"] = role
        if msg.get("content") is None:
            msg["content"] = ""
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            pending = []
            for c in msg["tool_calls"]:
                fn = (c or {}).get("function") or {}
                call_id = (c or {}).get("id") or new_call_id()
                name = fn.get("name") or ""
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": parse_arguments(fn.get("arguments")),
                        },
                    }
                )
                pending.append({"id": call_id, "name": name})
            msg["tool_calls"] = calls
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            known = {p["id"] for p in pending}
            if not call_id or call_id not in known:
                match = next(
                    (p for p in pending if p["name"] == msg.get("name")),
                    pending[0] if pending else None,
                )
                call_id = match["id"] if match else (call_id or new_call_id())
            pending = [p for p in pending if p["id"] != call_id]
            msg["tool_call_id"] = call_id
        else:
            pending = []
        out.append(msg)
    return out


def public_keys(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Drop provider-private ``_`` keys before sending to another backend."""
    return {k: v for k, v in msg.items() if not k.startswith("_")}


__all__ = [
    "ChatWithToolsResult",
    "LLMError",
    "LLMModelMissing",
    "LLMTimeout",
    "LLMUnavailable",
    "ToolCall",
    "canonical_history",
    "new_call_id",
    "parse_arguments",
    "public_keys",
]
