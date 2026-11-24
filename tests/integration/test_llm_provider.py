"""The LLM adapter: provider chain, failover, and cross-provider history.

Backends are stubbed at the module-function level, so nothing here talks
to xAI, Anthropic or a real Ollama daemon.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from api.ai import claude, grok, llm, ollama
from api.ai.llm_types import ChatWithToolsResult, ToolCall, canonical_history
from api.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_health():
    llm.reset_health()
    yield
    llm.reset_health()


@pytest.fixture
def full_chain():
    s = get_settings()
    with patch.object(s, "AI_LLM_PROVIDERS", "grok,claude,ollama"), patch.object(
        s, "ANTHROPIC_API_KEY", "sk-ant-api03-real-looking-test-key"
    ), patch.object(s, "AI_OLLAMA_MODEL", "gemma4:31b"), patch.object(
        s, "AI_OLLAMA_BACKUP_MODEL", "muse-glimmer"
    ), patch.object(s, "AI_OLLAMA_FAST_MODEL", "gemma4:e2b"), patch.object(
        s, "AI_LLM_FAILOVER_COOLDOWN_S", 60.0
    ):
        yield


def _stub(monkeypatch, backend, name, fn):
    monkeypatch.setattr(backend, name, fn)


# ---------------------------------------------------------------------------
# Chain + model selection
# ---------------------------------------------------------------------------


def test_test_env_is_grok_only():
    assert [b.NAME for b in llm.chain()] == ["grok"]
    assert llm.provider() == "grok"
    assert llm._model().startswith("grok")
    assert llm.fast_model().startswith("grok")
    assert llm.ollama_warmup_enabled() is False


def test_resolve_model_ignores_tags_owned_by_other_providers():
    assert llm.resolve_model("llama3.2:3b", fast=True) == get_settings().GROK_FAST_MODEL
    assert llm.resolve_model("gemma4:26b") == get_settings().GROK_MODEL
    assert llm.resolve_model("grok-4.6") == "grok-4.6"
    assert llm.resolve_model("", fast=True) == llm.fast_model()


def test_placeholder_claude_key_is_skipped(full_chain):
    with patch.object(get_settings(), "ANTHROPIC_API_KEY", "sk-ant-xxx..."):
        assert [b.NAME for b in llm.chain()] == ["grok", "ollama"]
    assert [b.NAME for b in llm.chain()] == ["grok", "claude", "ollama"]
    assert llm.ollama_warmup_enabled() is True


def test_local_tiers_have_backups(full_chain):
    assert ollama.tier_models("heavy") == ["gemma4:31b", "muse-glimmer"]
    assert ollama.tier_models("fast") == ["gemma4:e2b", "gemma4:31b", "muse-glimmer"]
    assert ollama.warm_models() == ["gemma4:31b", "gemma4:e2b"]


# ---------------------------------------------------------------------------
# Failover
# ---------------------------------------------------------------------------


async def test_offline_falls_through_to_local_and_skips_cloud_after(monkeypatch, full_chain):
    calls: list[str] = []

    async def grok_down(**kw):
        calls.append(f"grok/{kw['model']}")
        raise grok.GrokUnavailable("xAI at https://api.x.ai/v1 is not reachable")

    async def claude_down(**kw):
        calls.append(f"claude/{kw['model']}")
        raise claude.ClaudeUnavailable("Anthropic is not reachable")

    async def local_ok(**kw):
        calls.append(f"ollama/{kw['model']}")
        return "ok"

    _stub(monkeypatch, grok, "chat_oneshot", grok_down)
    _stub(monkeypatch, claude, "chat_oneshot", claude_down)
    _stub(monkeypatch, ollama, "chat_oneshot", local_ok)

    assert await llm.chat_oneshot(system="s", user="u") == "ok"
    assert calls == ["grok/grok-4.3", "claude/claude-haiku-5-5", "ollama/gemma4:e2b"]
    assert llm.provider() == "ollama"

    calls.clear()
    assert await llm.chat_oneshot(system="s", user="u") == "ok"
    assert calls == ["ollama/gemma4:e2b"], "cloud should be skipped during cooldown"

    llm.reset_health()
    assert llm.provider() == "grok"


async def test_missing_local_model_tries_backup(monkeypatch, full_chain):
    tried: list[str] = []

    async def grok_down(*a, **kw):
        raise grok.GrokUnavailable("offline")

    async def claude_down(*a, **kw):
        raise claude.ClaudeUnavailable("offline")

    async def local(messages, **kw):
        tried.append(kw["model"])
        if kw["model"] == "gemma4:31b":
            raise ollama.OllamaModelMissing("Model 'gemma4:31b' is not pulled.")
        return ChatWithToolsResult(content="hi", model=kw["model"], provider="ollama")

    _stub(monkeypatch, grok, "chat_with_tools", grok_down)
    _stub(monkeypatch, claude, "chat_with_tools", claude_down)
    _stub(monkeypatch, ollama, "chat_with_tools", local)

    out = await llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert out.model == "muse-glimmer"
    assert tried == ["gemma4:31b", "muse-glimmer"]
    assert llm.provider() == "ollama", "a missing model must not mark Ollama down"


async def test_timeout_moves_on_without_marking_down(monkeypatch, full_chain):
    async def grok_slow(*a, **kw):
        raise grok.GrokTimeout("read timeout")

    async def claude_ok(*a, **kw):
        return "from claude"

    _stub(monkeypatch, grok, "generate", grok_slow)
    _stub(monkeypatch, claude, "generate", claude_ok)

    assert await llm.generate("hello") == "from claude"
    assert llm.provider() == "grok"


async def test_all_providers_down_raises_unavailable(monkeypatch, full_chain):
    async def down(*a, **kw):
        raise llm.LLMUnavailable("down")

    for backend in (grok, claude, ollama):
        _stub(monkeypatch, backend, "generate", down)
    with pytest.raises(llm.LLMUnavailable, match="All LLM providers failed"):
        await llm.generate("hello")


async def test_generate_with_fallback_reports_model_used(monkeypatch, full_chain):
    async def grok_down(*a, **kw):
        raise grok.GrokUnavailable("offline")

    async def claude_down(*a, **kw):
        raise claude.ClaudeUnavailable("offline")

    async def local(prompt, **kw):
        return f"ok from {kw['model']}"

    _stub(monkeypatch, grok, "generate", grok_down)
    _stub(monkeypatch, claude, "generate", claude_down)
    _stub(monkeypatch, ollama, "generate", local)

    text, used = await llm.generate_with_fallback("p", primary_model=llm.fast_model())
    assert used == "gemma4:e2b"
    assert text == "ok from gemma4:e2b"


async def test_stream_falls_back_before_first_chunk(monkeypatch, full_chain):
    async def grok_down(*a, **kw):
        raise grok.GrokUnavailable("offline")
        yield ""  # pragma: no cover

    async def claude_down(*a, **kw):
        raise claude.ClaudeUnavailable("offline")
        yield ""  # pragma: no cover

    async def local(*a, **kw):
        for piece in ("hel", "lo"):
            yield piece

    _stub(monkeypatch, grok, "chat_stream", grok_down)
    _stub(monkeypatch, claude, "chat_stream", claude_down)
    _stub(monkeypatch, ollama, "chat_stream", local)

    chunks = [c async for c in llm.chat_stream([{"role": "user", "content": "hi"}])]
    assert "".join(chunks) == "hello"


async def test_tool_calls_always_get_ids(monkeypatch):
    async def no_ids(*a, **kw):
        return ChatWithToolsResult(
            content="",
            tool_calls=[ToolCall(name="lookup_person", arguments={"name": "Ben"})],
            model=kw["model"],
        )

    _stub(monkeypatch, grok, "chat_with_tools", no_ids)
    out = await llm.chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert out.tool_calls[0].id.startswith("call_")
    assert out.history_message()["tool_calls"][0]["id"] == out.tool_calls[0].id


# ---------------------------------------------------------------------------
# History shape across providers
# ---------------------------------------------------------------------------


def _history_without_ids():
    return [
        {"role": "user", "content": "Who is Ben?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "lookup_person", "arguments": '{"name": "Ben"}'}},
                {"function": {"name": "list_goals", "arguments": {}}},
            ],
        },
        {"role": "tool", "name": "lookup_person", "content": '{"id": 2}'},
        {"role": "tool", "name": "list_goals", "content": "[]"},
    ]


def test_canonical_history_mints_and_pairs_ids():
    out = canonical_history(_history_without_ids())
    ids = [c["id"] for c in out[1]["tool_calls"]]
    assert all(ids)
    assert [out[2]["tool_call_id"], out[3]["tool_call_id"]] == ids
    assert out[1]["tool_calls"][0]["function"]["arguments"] == {"name": "Ben"}


def test_grok_wire_format_serialises_arguments():
    wire = grok._to_wire(_history_without_ids(), "sys")
    assert wire[0] == {"role": "system", "content": "sys"}
    call = wire[2]["tool_calls"][0]
    assert json.loads(call["function"]["arguments"]) == {"name": "Ben"}
    assert wire[3]["tool_call_id"] == call["id"]


def test_claude_wire_format_merges_tool_results_and_keeps_thinking():
    system, turns = claude._to_wire(_history_without_ids(), "sys")
    assert system == "sys"
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    uses = [b for b in turns[1]["content"] if b["type"] == "tool_use"]
    results = turns[2]["content"]
    assert [r["tool_use_id"] for r in results] == [u["id"] for u in uses]
    assert uses[0]["input"] == {"name": "Ben"}

    blocks = [
        {"type": "thinking", "thinking": "...", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup_person", "input": {}},
    ]
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "function": {"name": "lookup_person", "arguments": {}}}],
            "_claude_content": blocks,
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "{}"},
    ]
    _, turns = claude._to_wire(history, None)
    assert turns[1]["content"] == blocks


def test_ollama_wire_drops_private_keys():
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "function": {"name": "x", "arguments": {}}}],
            "_claude_content": [{"type": "thinking"}],
        },
    ]
    wire = ollama._to_wire(history, None)
    assert "_claude_content" not in wire[1]


def test_claude_short_calls_turn_thinking_off():
    assert claude._short_call_options("claude-haiku-5-5", 8)["thinking"] == {"type": "disabled"}
    assert claude._short_call_options("claude-sonnet-5-5", 8)["thinking"] == {"type": "between_tools"}
    opus = claude._short_call_options("claude-opus-5-5", 8)
    assert "thinking" not in opus and opus["max_tokens"] > 8
