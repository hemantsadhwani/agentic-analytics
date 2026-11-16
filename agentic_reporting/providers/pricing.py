"""USD per million tokens, for cost *estimates* (list price, not an invoice).

Each row carries its source. Bedrock prices are per region; the rows below are the ones
this repo actually runs. Ollama runs locally and costs 0 (it never calls this module).

**Fails closed** (since 2026-11-15): a model with no row raises ``UnknownPrice``. Before,
an unknown model estimated to $0, so a ``--max-usd`` budget could never stop it. Providers
call ``require_price`` before the first request, so the error costs nothing.
"""

from __future__ import annotations

from agentic_reporting.providers.base import ProviderUnavailable

# model -> (input, output, cache_read, cache_write) USD per million tokens
PRICES: dict[str, tuple[float, float, float, float]] = {
    # Anthropic list price (Anthropic API; Bedrock bills Claude at the same list price).
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    # AWS Bedrock on-demand, Standard tier, ap-south-1 (Mumbai), in-region. Source: AWS Price
    # List API (pricing:GetProducts, regionCode=ap-south-1), read 2026-11-15; DeepSeek also
    # matches aws.amazon.com/bedrock/pricing. Candidates for eval/model_bench.py.
    "deepseek.v3.2": (0.74, 2.22, 0.0, 0.0),
    "google.gemma-3-4b-it": (0.05, 0.09, 0.0, 0.0),
    "google.gemma-3-12b-it": (0.11, 0.34, 0.0, 0.0),
    "google.gemma-3-27b-it": (0.27, 0.45, 0.0, 0.0),
    "mistral.ministral-3-3b-instruct": (0.12, 0.12, 0.0, 0.0),
    "mistral.ministral-3-8b-instruct": (0.18, 0.18, 0.0, 0.0),
    "mistral.ministral-3-14b-instruct": (0.24, 0.24, 0.0, 0.0),
    "nvidia.nemotron-nano-9b-v2": (0.07, 0.27, 0.0, 0.0),
    "nvidia.nemotron-nano-3-30b": (0.07, 0.28, 0.0, 0.0),
    "openai.gpt-oss-20b-1:0": (0.08, 0.35, 0.0, 0.0),
    "openai.gpt-oss-120b-1:0": (0.18, 0.71, 0.0, 0.0),
    "qwen.qwen3-32b-v1:0": (0.18, 0.71, 0.0, 0.0),
    "qwen.qwen3-next-80b-a3b": (0.18, 1.41, 0.0, 0.0),
    "zai.glm-4.7-flash": (0.08, 0.48, 0.0, 0.0),
    # Embedding models (input tokens only), ap-south-1, in-region. Source: AWS Price List API,
    # read 2026-11-16 (Titan: service AmazonBedrock; Cohere: AmazonBedrockFoundationModels).
    "amazon.titan-embed-text-v2:0": (0.024, 0.0, 0.0, 0.0),
    "cohere.embed-multilingual-v3": (0.10, 0.0, 0.0, 0.0),
    "cohere.embed-english-v3": (0.10, 0.0, 0.0, 0.0),
}
PRICE_LABEL = "list price"


class UnknownPrice(ProviderUnavailable):
    """No price row for this model: refuse to run rather than report a $0 cost."""


def _row(model: str) -> tuple[float, float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    if "haiku-4-5" in model:          # e.g. in.anthropic.claude-haiku-4-5-20251001-v1:0
        return PRICES["claude-haiku-4-5"]
    if model.endswith("deepseek.v3.2"):
        return PRICES["deepseek.v3.2"]
    return None


def has_price(model: str) -> bool:
    return _row(model) is not None


def require_price(model: str) -> None:
    if not has_price(model):
        raise UnknownPrice(
            f"no price for model {model!r} in agentic_reporting/providers/pricing.py; add a "
            "row (with its source) before running it, so the cost budget can be enforced")


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated USD for one call at list price. Raises ``UnknownPrice`` for an unknown model."""
    row = _row(model)
    if row is None:
        require_price(model)
    assert row is not None
    p_in, p_out, p_read, p_write = row
    return (
        input_tokens * p_in
        + output_tokens * p_out
        + cache_read_tokens * p_read
        + cache_write_tokens * p_write
    ) / 1_000_000
