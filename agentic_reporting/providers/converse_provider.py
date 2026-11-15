"""Any Bedrock model through the Converse API. Default: Gemma 3 12B, in-region in Mumbai.

Why this provider exists: the Anthropic-SDK Bedrock provider speaks only Claude. The
Converse API is model-agnostic, so the cheapest model that passes the checker can be the
default and Claude becomes an escalation tier (``LLM_MODEL``), with no code change.

Defaults (2026-11-15, chosen by ``eval/model_bench.py``; result in
``eval/results/model_bench_2026-11-15.json``):

* model ``google.gemma-3-12b-it``: on-demand in ``ap-south-1`` itself, so the data stays in
  India. Of 8 in-region models x 2 golden reports x 3 repeats it passed 6/6 with every
  section right first time, full required-fact coverage and the fewest attribution warnings
  of the cheap models: $0.00058 per report ($0.11 in / $0.34 out per million tokens).
  DeepSeek V3.2 also passed 6/6 at $0.00424 per report.
* region ``ap-south-1`` (``AWS_REGION`` overrides). Credentials come from the standard AWS
  chain (``AWS_PROFILE``, an ECS task role); nothing is stored in the repo.
* escalation example: ``LLM_MODEL=in.anthropic.claude-haiku-4-5-20251001-v1:0``, the
  India-only inference profile.

Structured output ladder, strongest first (``LLMResponse.structured_mode``):

1. ``native``: a single tool whose input schema is the Pydantic schema, forced with
   ``toolChoice``; the tool input is validated.
2. ``prompt``: the schema in the system prompt, the reply validated, one repair round-trip.

The cost is estimated from the token counts Bedrock returns, at the price row for the model.
A model with no price row is refused before any call (``pricing.require_price``).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ValidationError

from agentic_reporting.providers import pricing
from agentic_reporting.providers.base import (
    REPAIR_TEMPLATE,
    LLMResponse,
    ProviderUnavailable,
    StructuredMode,
    schema_instruction,
    short_error,
    validate_text,
)

DEFAULT_CONVERSE_MODEL = "google.gemma-3-12b-it"
DEFAULT_CONVERSE_REGION = "ap-south-1"
TOOL_NAME = "submit"


def _messages(messages: list[dict]) -> list[dict[str, Any]]:
    return [{"role": m["role"], "content": [{"text": str(m.get("content") or "(empty)")}]}
            for m in messages]


def _text(resp: dict[str, Any]) -> str:
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(p.get("text", "") for p in parts if "text" in p)


def _tool_input(resp: dict[str, Any]) -> dict[str, Any] | None:
    for p in resp.get("output", {}).get("message", {}).get("content", []):
        if "toolUse" in p:
            return p["toolUse"].get("input")
    return None


def tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The Pydantic JSON schema made strict and small for a tool definition.

    * every property is required: Pydantic marks fields with defaults optional, and on the
      first real run DeepSeek then skipped ``numbers_used`` and every number was uncited;
    * ``$ref`` / ``$defs`` are inlined, and ``title`` keys dropped: fewer tokens per call,
      and some models handle a flat schema more reliably.
    """
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(raw)


def _is_unsupported(err: Exception) -> bool:
    """A 400 about the request shape (tools, temperature), not a throttle or an outage."""
    code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
    return code == "ValidationException"


class BedrockConverseProvider:
    name = "bedrock-converse"
    cost_label = pricing.PRICE_LABEL

    def __init__(self, model: str | None = None, region: str | None = None,
                 client: Any = None) -> None:
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_CONVERSE_MODEL
        self.region = region or os.environ.get("AWS_REGION") or DEFAULT_CONVERSE_REGION
        self._client = client
        self._temperature: float | None = 0.0       # dropped if the model refuses it

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as e:   # pragma: no cover - depends on extras install
                raise ProviderUnavailable("boto3 is not installed: pip install -e '.[llm]'") from e
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def _call(self, **kw: Any) -> dict[str, Any]:
        cfg = {"maxTokens": kw.pop("max_tokens")}
        if self._temperature is not None:
            cfg["temperature"] = self._temperature
        try:
            return self.client.converse(modelId=self.model, inferenceConfig=cfg, **kw)
        except Exception as e:
            if _is_unsupported(e) and "temperature" in str(e) and self._temperature is not None:
                self._temperature = None
                return self._call(max_tokens=cfg["maxTokens"], **kw)
            raise

    def complete(self, *, system: str, messages: list[dict], max_tokens: int,
                 schema: type[BaseModel] | None = None,
                 cache_system: bool = True) -> LLMResponse:
        pricing.require_price(self.model)            # fail closed before spending anything
        usage = [0, 0]

        def call(sys_text: str, msgs: list[dict], **extra: Any) -> dict[str, Any]:
            resp = self._call(system=[{"text": sys_text}], messages=_messages(msgs),
                              max_tokens=max_tokens, **extra)
            u = resp.get("usage", {})
            usage[0] += int(u.get("inputTokens", 0))
            usage[1] += int(u.get("outputTokens", 0))
            return resp

        if schema is None:
            resp = call(system, messages)
            return self._response(_text(resp), None, usage, "none")

        # 1. native: one forced tool whose input schema is the Pydantic schema
        tool = {"tools": [{"toolSpec": {
                    "name": TOOL_NAME,
                    "description": "Submit the answer as structured data.",
                    "inputSchema": {"json": tool_schema(schema)}}}],
                "toolChoice": {"tool": {"name": TOOL_NAME}}}
        try:
            resp = call(system, messages, toolConfig=tool)
            data = _tool_input(resp)
            if data is not None:
                try:
                    return self._response(_text(resp), schema.model_validate(data), usage,
                                          "native")
                except ValidationError:
                    pass
        except Exception as e:
            if not _is_unsupported(e):
                raise

        # 2. prompt: schema in the system prompt, validate, one repair round-trip
        prompt_system = system + schema_instruction(schema)
        resp = call(prompt_system, messages)
        text = _text(resp)
        try:
            return self._response(text, validate_text(schema, text), usage, "prompt")
        except ValidationError as err:
            repair = [*messages, {"role": "assistant", "content": text or "(empty)"},
                      {"role": "user", "content": REPAIR_TEMPLATE.format(error=short_error(err))}]
            resp = call(prompt_system, repair)
            text = _text(resp)
            return self._response(text, validate_text(schema, text), usage, "prompt")

    def _response(self, text: str, parsed: Any, usage: list[int],
                  mode: StructuredMode) -> LLMResponse:
        return LLMResponse(
            text=text, parsed=parsed, provider=self.name, model=self.model,
            input_tokens=usage[0], output_tokens=usage[1],
            cost_usd=pricing.estimate_cost(self.model, usage[0], usage[1]),
            structured_mode=mode)
