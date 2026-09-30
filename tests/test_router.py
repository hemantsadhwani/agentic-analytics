"""Provider selection: explicit name, $LLM_PROVIDER, key present, else the offline fake."""

from __future__ import annotations

import pytest

from agentic_reporting.providers import get_provider
from agentic_reporting.providers.fake import FactsFakeProvider
from agentic_reporting.providers.router import resolve_name


def test_no_key_no_env_falls_back_to_fake_with_one_line(capsys):
    assert isinstance(get_provider(), FactsFakeProvider)
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "offline 'fake' provider" in err


def test_env_and_key_resolution(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    assert resolve_name() == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert resolve_name() == "ollama"
    assert resolve_name("bedrock") == "bedrock"


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("gpt")
