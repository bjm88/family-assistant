"""Provider selection for the live LLM facade."""

from __future__ import annotations

from unittest.mock import patch

from api.ai import llm
from api.config import get_settings


def test_default_provider_is_grok():
    assert get_settings().llm_provider == "grok"
    assert llm.provider() == "grok"
    assert llm._model().startswith("grok")
    assert llm.fast_model().startswith("grok")


def test_resolve_model_drops_ollama_tags_when_grok_is_live():
    assert llm.resolve_model("llama3.2:3b", fast=True).startswith("grok")
    assert llm.resolve_model("gemma4:26b").startswith("grok")
    assert llm.resolve_model("grok-4.6") == "grok-4.6"
    assert llm.resolve_model("", fast=True) == llm.fast_model()


def test_grok_api_key_alias():
    settings = get_settings()
    assert settings.grok_api_key, "GROK_API_KEY must resolve in tests"


def test_ollama_warmup_off_while_parked():
    with patch.object(get_settings(), "AI_OLLAMA_ENABLED", False):
        assert llm.ollama_warmup_enabled() is False
