"""One provider interface for every model call in agentic-reporting.

    from agentic_reporting.providers import get_provider
    provider = get_provider()                       # LLM_PROVIDER=fake|anthropic|bedrock|ollama
    resp = provider.complete(system=..., messages=[...], max_tokens=512, schema=MyModel)
    resp.parsed      # a validated MyModel instance, or the call raised

Same interface and env names as the author's yantra-research-lab ``llm_gateway`` (ADR-0004):
Anthropic direct, Claude on AWS Bedrock, or a local Ollama model, chosen by one env var.
Every structured reply is validated against a Pydantic schema before a caller sees it.
SDKs are imported lazily inside the provider that needs them.
"""

from agentic_reporting.providers.base import LLMResponse, Provider
from agentic_reporting.providers.router import PROVIDERS, get_provider
from agentic_reporting.providers.testing import FakeProvider

__all__ = ["PROVIDERS", "FakeProvider", "LLMResponse", "Provider", "get_provider"]
