"""Offline providers that write plausible prose from the facts in the prompt.

``FactsFakeProvider`` implements the ``Provider`` protocol and sees exactly what a real
model sees: it parses the ``QUERY_RESULT_JSON:`` block out of the user message and writes
one sentence per row ("North: revenue 1,234.5, units 310."). Every number it writes comes
from the facts table and is cited, so a clean run shows zero unsupported numbers.

``FabricatingProvider`` wraps it and appends a made-up number to the draft. It is the
negative control for the checker:

* ``FabricatingProvider()`` fabricates on every call, including the repair call, so the
  number survives to ``render`` and is published as ``[unverified]``;
* ``FabricatingProvider(repairable=True)`` fabricates only on the first draft; the repair
  prompt (which quotes the unsupported number back) gets a clean answer.

Deterministic, no key, no network, $0.
"""

from __future__ import annotations

import json
import math
from typing import Any

from agentic_reporting.data import is_number
from agentic_reporting.draft import FACTS_MARKER, REPAIR_MARKER, SectionDraft
from agentic_reporting.providers.base import LLMResponse

MAX_ROWS = 6
FABRICATED = "987,654.3"


def fmt(v: float) -> str:
    """Human formatting that stays within the checker's 0.5% tolerance."""
    if isinstance(v, int) or float(v).is_integer():
        return f"{int(v):,}"
    a = abs(v)
    decimals = 1 if a >= 100 else 2 if a >= 1 else max(3, 2 - math.floor(math.log10(a)) + 1)
    s = f"{v:,.{decimals}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _label(col: str) -> str:
    return col.replace("_", " ")


def prose(columns: list[str], rows: list[list[Any]]) -> tuple[str, list[dict[str, Any]]]:
    """One sentence per row (first ``MAX_ROWS``), every number cited by fact key."""
    if not rows:
        return "The query returned no rows for this period.", []
    sentences: list[str] = []
    used: list[dict[str, Any]] = []
    for i, row in enumerate(rows[:MAX_ROWS]):
        cells = list(zip(columns, row, strict=True))
        lead = None
        if cells and isinstance(cells[0][1], str):
            lead = cells[0][1]
            cells = cells[1:]
        parts = []
        for col, val in cells:
            if val is None:
                continue
            if is_number(val):
                parts.append(f"{_label(col)} {fmt(val)}")
                used.append({"value": float(val), "fact_key": f"{col}[{i}]"})
            else:
                parts.append(f"{_label(col)} {val}")
        body = ", ".join(parts) if parts else "no values"
        sentence = f"{lead}: {body}." if lead else f"{body[0].upper()}{body[1:]}."
        sentences.append(sentence)
    return " ".join(sentences), used


def payload_from(messages: list[dict]) -> dict[str, Any]:
    """The query-result JSON the drafting prompt embeds after ``QUERY_RESULT_JSON:``."""
    content = str(messages[-1].get("content", ""))
    lines = content.splitlines()
    for n, line in enumerate(lines):
        if line.strip() == FACTS_MARKER and n + 1 < len(lines):
            return json.loads(lines[n + 1])
    raise ValueError("no QUERY_RESULT_JSON block in the prompt")


def is_repair(messages: list[dict]) -> bool:
    return REPAIR_MARKER in str(messages[-1].get("content", ""))


class FactsFakeProvider:
    name = "fake"
    model = "facts-fake-1"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, messages: list[dict], max_tokens: int,
                 schema: Any = None, cache_system: bool = True) -> LLMResponse:
        self.calls.append({"system": system, "messages": messages})
        p = payload_from(messages)
        text, used = prose(p["columns"], p["rows"])
        text = self.alter(text, messages)
        draft = SectionDraft(text=text, numbers_used=used)
        in_tok = (len(system) + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        return LLMResponse(text=draft.model_dump_json(), parsed=draft, provider=self.name,
                           model=self.model, input_tokens=in_tok,
                           output_tokens=len(text) // 4, cost_usd=0.0,
                           structured_mode="native")

    def alter(self, text: str, messages: list[dict]) -> str:
        return text


class FabricatingProvider(FactsFakeProvider):
    """Adds a number that no query returned (the eval's and the tests' negative control)."""

    model = "fabricating-fake-1"

    def __init__(self, repairable: bool = False) -> None:
        super().__init__()
        self.repairable = repairable

    def alter(self, text: str, messages: list[dict]) -> str:
        if self.repairable and is_repair(messages):
            return text
        return f"{text} Analysts expect {FABRICATED} next quarter."
