"""Pick a provider from the environment: ``LLM_PROVIDER`` and ``LLM_MODEL``.

Resolution order for the provider name:

1. the explicit ``name`` argument (the CLI's ``--provider``);
2. ``$LLM_PROVIDER``;
3. ``anthropic`` if ``$ANTHROPIC_API_KEY`` is set;
4. otherwise ``fake`` (the offline ``FactsFakeProvider``), with one line on stderr saying so.

The fallback means a fresh clone runs end to end with no key and no network.
"""

from __future__ import annotations

import os
import sys

from agentic_reporting.providers.base import Provider

PROVIDERS = ("fake", "anthropic", "bedrock", "bedrock-converse", "ollama")
FALLBACK_NOTE = ("note: no LLM_PROVIDER and no ANTHROPIC_API_KEY set; using the offline "
                 "'fake' provider (deterministic, $0, no network)")


def resolve_name(name: str | None = None, *, announce: bool = True) -> str:
    """The provider name to use, applying the fallback above."""
    chosen = (name or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if chosen:
        return chosen
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if announce:
        print(FALLBACK_NOTE, file=sys.stderr)
    return "fake"


def get_provider(name: str | None = None, model: str | None = None) -> Provider:
    """Build the provider named by ``name`` / ``$LLM_PROVIDER`` (see the module docstring).

    ``model`` (or ``$LLM_MODEL``) overrides the provider's default model. No network
    call happens here; credentials are checked on the first ``complete``.
    """
    chosen = resolve_name(name)
    model = model or os.environ.get("LLM_MODEL") or None
    if chosen == "fake":
        from agentic_reporting.providers.fake import FactsFakeProvider
        return FactsFakeProvider()
    if chosen == "anthropic":
        from agentic_reporting.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model)
    if chosen == "bedrock":
        from agentic_reporting.providers.bedrock_provider import BedrockProvider
        return BedrockProvider(model=model)
    if chosen == "bedrock-converse":
        from agentic_reporting.providers.converse_provider import BedrockConverseProvider
        return BedrockConverseProvider(model=model)
    if chosen == "ollama":
        from agentic_reporting.providers.ollama_provider import OllamaProvider
        return OllamaProvider(model=model)
    raise ValueError(f"unknown LLM provider {chosen!r}; valid: {', '.join(PROVIDERS)}")
