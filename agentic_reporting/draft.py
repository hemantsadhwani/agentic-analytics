"""One structured LLM call per section, through the ``providers`` ``Provider`` protocol.

The model sees the section prompt and the query result (columns, rows, and the flat
``facts`` table keyed ``<column>[<row>]``). It must return a ``SectionDraft``: the prose,
plus every number it used with the fact key it came from. The provider's structured-output
ladder (native / json_schema / prompt + one repair round-trip) validates the shape.
``check.py`` then verifies the numbers without trusting the model's citations.

The system prompt is byte-stable (cacheable); everything section-specific is in the user turn.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from agentic_reporting.budget import charge, denied

DRAFT_MAX_TOKENS = 700
FACTS_MARKER = "QUERY_RESULT_JSON:"
REPAIR_MARKER = "REPAIR:"

SYSTEM_PROMPT = """You write one section of a data report from the result of a SQL query.

Rules, in order of importance:
1. Use ONLY numbers that appear in the facts table. Never compute, round to a different
   precision, estimate, sum or invent a number. Copy each value as given (you may drop
   trailing decimals only if the result stays within 0.5% of the fact).
2. Cite every number you write in numbers_used, as {"value": <number>, "fact_key": "<key>"},
   using the exact key from the facts table (for example "revenue[0]").
3. Write no other digits at all. Spell small counts under twenty that are not facts in
   words ("three regions", not "3 regions"). Every other number, digits or words, is
   checked. Do not number your sentences. Do not write fact keys in the text.
4. Dates and years may be written only as they appear in the rows.
5. Stay within the word limit. Plain English, no headings, no bullet lists, no markdown tables.

Respond with ONLY a JSON object: {"text": "<prose>", "numbers_used": [{"value": <number>,
"fact_key": "<key>"}, ...]}"""


class NumberRef(BaseModel):
    value: float
    fact_key: str


class SectionDraft(BaseModel):
    text: str = Field(min_length=1)
    numbers_used: list[NumberRef] = Field(default_factory=list)


def user_message(section: dict[str, Any], query: dict[str, Any],
                 unsupported: list[str] | None = None, previous: str | None = None) -> str:
    payload = {"columns": query["columns"], "rows": query["rows"], "facts": query["facts"],
               "truncated": query.get("truncated", False)}
    parts = [
        f"Section: {section['heading']}",
        f"Instruction: {section['prompt']}",
        f"Word limit: {section['max_words']}",
        "",
        FACTS_MARKER,
        json.dumps(payload, default=str),
    ]
    if unsupported:
        parts += [
            "",
            f"{REPAIR_MARKER} your previous draft was rejected by the number checker.",
            f"Previous draft: {previous or ''}",
            f"The checker rejected: {json.dumps(unsupported)}.",
            "Rewrite the section using only numbers from the facts table, or no number at all,",
            "and cite every number you write in numbers_used with its exact fact key.",
        ]
    parts += ["", "Return the JSON object only."]
    return "\n".join(parts)


def draft_section(provider: Any, section: dict[str, Any], query: dict[str, Any],
                  budget: dict[str, Any], unsupported: list[str] | None = None,
                  previous: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Draft one section. Returns ``(draft_dict, None)`` or ``(None, reason)``.

    Budget exhausted -> no call, ``(None, "budget ...")``. A reply that fails schema
    validation even after the provider's repair round-trip, or a provider that is not
    available here, is recorded as a failure for this section (the run continues and the
    gate shows it). Any other exception (network, 5xx) propagates so the node's
    ``RetryPolicy`` can retry it.
    """
    from agentic_reporting.providers.base import ProviderUnavailable

    why = denied(budget)
    if why:
        return None, f"budget exhausted ({why})"
    try:
        resp = provider.complete(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": user_message(section, query, unsupported, previous)}],
            max_tokens=DRAFT_MAX_TOKENS,
            schema=SectionDraft,
            cache_system=True,
        )
    except (ValueError, ProviderUnavailable) as e:   # ValidationError is a ValueError
        return None, f"{type(e).__name__}: {str(e)[:160]}"
    charge(budget, resp.cost_usd)
    parsed = resp.parsed
    if not isinstance(parsed, SectionDraft):
        return None, "reply did not parse as a SectionDraft"
    out = parsed.model_dump()
    out["provider"] = f"{resp.provider}/{resp.model}"
    out["structured_mode"] = resp.structured_mode
    return out, None
