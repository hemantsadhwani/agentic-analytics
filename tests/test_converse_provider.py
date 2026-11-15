"""Bedrock Converse provider, offline: a stub client stands in for boto3."""

from __future__ import annotations

import pytest

from agentic_reporting.draft import SectionDraft
from agentic_reporting.providers import pricing
from agentic_reporting.providers.converse_provider import BedrockConverseProvider


class _Err(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.response = {"Error": {"Code": "ValidationException"}}


class Stub:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def converse(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def reply(text=None, tool=None, i=100, o=20):
    content = ([{"text": text}] if text is not None else []) + \
              ([{"toolUse": {"name": "submit", "input": tool}}] if tool is not None else [])
    return {"output": {"message": {"content": content}},
            "usage": {"inputTokens": i, "outputTokens": o}}


GOOD = {"text": "Revenue was 111,850.2.", "numbers_used": [{"value": 111850.2,
                                                           "fact_key": "revenue[0]"}]}
MSG = [{"role": "user", "content": "draft"}]


def test_defaults_are_the_benchmark_winner_in_mumbai(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    p = BedrockConverseProvider(client=Stub([]))
    assert (p.model, p.region) == ("google.gemma-3-12b-it", "ap-south-1")


def test_native_tool_rung_and_cost():
    stub = Stub([reply(tool=GOOD, i=1000, o=100)])
    r = BedrockConverseProvider(model="deepseek.v3.2", client=stub).complete(
        system="s", messages=MSG, max_tokens=300, schema=SectionDraft)
    assert r.structured_mode == "native" and isinstance(r.parsed, SectionDraft)
    assert stub.calls[0]["toolConfig"]["toolChoice"] == {"tool": {"name": "submit"}}
    assert r.cost_usd == pytest.approx((1000 * 0.74 + 100 * 2.22) / 1e6)


def test_falls_back_to_prompt_then_repairs():
    import json
    stub = Stub([_Err("tool use is not supported"), reply(text="not json"),
                 reply(text=json.dumps(GOOD))])
    r = BedrockConverseProvider(model="deepseek.v3.2", client=stub).complete(
        system="s", messages=MSG, max_tokens=300, schema=SectionDraft)
    assert r.structured_mode == "prompt" and r.parsed.numbers_used[0].fact_key == "revenue[0]"
    assert r.input_tokens == 200                     # both prompt calls counted, not the 400


def test_drops_temperature_when_the_model_refuses_it():
    stub = Stub([_Err("This model doesn't support the temperature field."), reply(text="ok")])
    r = BedrockConverseProvider(model="deepseek.v3.2", client=stub).complete(
        system="s", messages=MSG, max_tokens=50)
    assert r.text == "ok" and "temperature" not in stub.calls[1]["inferenceConfig"]


def test_unknown_price_refused_before_any_call():
    stub = Stub([reply(text="never")])
    with pytest.raises(pricing.UnknownPrice):
        BedrockConverseProvider(model="some.unpriced-model", client=stub).complete(
            system="s", messages=MSG, max_tokens=50)
    assert stub.calls == []


def test_pricing_fails_closed():
    with pytest.raises(pricing.UnknownPrice):
        pricing.estimate_cost("mystery-model", 10, 10)
    assert pricing.estimate_cost("in.anthropic.claude-haiku-4-5-20251001-v1:0", 1_000_000, 0) == 1.0


def test_tool_schema_is_strict_flat_and_small():
    import json

    from agentic_reporting.providers.converse_provider import tool_schema
    s = tool_schema(SectionDraft)
    assert s["required"] == ["text", "numbers_used"]          # was ["text"]: model skipped it
    item = s["properties"]["numbers_used"]["items"]
    assert item["required"] == ["value", "fact_key"] and "$ref" not in json.dumps(s)
    assert "title" not in json.dumps(s)
    assert len(json.dumps(s)) < len(json.dumps(SectionDraft.model_json_schema()))
