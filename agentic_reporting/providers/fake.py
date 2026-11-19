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


# --- text-to-SQL ---------------------------------------------------------------------------
# Hand-written SQL for the golden schema questions (eval/datasets/golden/schema_questions_*).
# Written against the governed metric expressions and the linked join paths, in a different
# style from the golden files' reference SQL (which hard-codes the month), so the eval
# compares two independent computations. Keyed by the lower-cased question without "?".

_LATEST_SALES = "latest AS (SELECT MAX(substr(order_date, 1, 7)) AS m FROM orders)"
_IN_LATEST_SALES = "JOIN latest ON substr(orders.order_date, 1, 7) = latest.m"
_LATEST_OPS = "latest AS (SELECT MAX(substr(opened_at, 1, 7)) AS m FROM incidents)"
_IN_LATEST_OPS = "JOIN latest ON substr(incidents.opened_at, 1, 7) = latest.m"

GOLDEN_SQL: dict[str, tuple[str, list[str]]] = {
    "which region had the highest net revenue last month": (
        f"WITH {_LATEST_SALES} SELECT orders.region AS region, "
        f"ROUND(SUM(orders.revenue), 2) AS net_revenue FROM orders {_IN_LATEST_SALES} "
        "GROUP BY orders.region ORDER BY net_revenue DESC LIMIT 1", ["net_revenue"]),
    "how many orders were placed last month": (
        f"WITH {_LATEST_SALES} SELECT COUNT(DISTINCT orders.order_id) AS orders "
        f"FROM orders {_IN_LATEST_SALES}", ["orders"]),
    "what was the month-over-month change in sales last month": (
        "WITH monthly AS (SELECT substr(orders.order_date, 1, 7) AS month, "
        "ROUND(SUM(orders.revenue), 2) AS net_revenue FROM orders GROUP BY month), "
        "ranked AS (SELECT month, net_revenue, ROW_NUMBER() OVER (ORDER BY month DESC) AS rn "
        "FROM monthly) SELECT cur.month AS month, "
        "ROUND(100.0 * (cur.net_revenue - prev.net_revenue) / prev.net_revenue, 1) "
        "AS mom_change_pct FROM ranked AS cur JOIN ranked AS prev ON cur.rn = 1 AND prev.rn = 2",
        ["mom_change_pct", "net_revenue"]),
    "net revenue by customer segment last month": (
        f"WITH {_LATEST_SALES} SELECT customers.segment AS segment, "
        "ROUND(SUM(orders.revenue), 2) AS net_revenue FROM orders "
        f"JOIN customers ON orders.customer_id = customers.customer_id {_IN_LATEST_SALES} "
        "GROUP BY customers.segment ORDER BY net_revenue DESC", ["net_revenue"]),
    "which product category earned the most turnover last month": (
        f"WITH {_LATEST_SALES} SELECT products.category AS category, "
        "ROUND(SUM(orders.revenue), 2) AS net_revenue FROM orders "
        f"JOIN products ON orders.product = products.product {_IN_LATEST_SALES} "
        "GROUP BY products.category ORDER BY net_revenue DESC LIMIT 1", ["net_revenue"]),
    "how many incidents were opened last month": (
        f"WITH {_LATEST_OPS} SELECT COUNT(incidents.incident_id) AS incident_count "
        f"FROM incidents {_IN_LATEST_OPS}", ["incident_count"]),
    "what was the mttr in hours by severity last month": (
        f"WITH {_LATEST_OPS} SELECT incidents.severity AS severity, "
        "ROUND(AVG(incidents.minutes_to_restore) / 60.0, 3) AS mttr_hours "
        f"FROM incidents {_IN_LATEST_OPS} GROUP BY incidents.severity ORDER BY severity",
        ["mttr_hours"]),
    "what share of incidents were sev1 last month": (
        f"WITH {_LATEST_OPS} SELECT ROUND(100.0 * SUM(incidents.severity = 'SEV1') "
        f"/ COUNT(incidents.incident_id), 2) AS sev1_share FROM incidents {_IN_LATEST_OPS}",
        ["sev1_share"]),
    "which team had the most incidents last month": (
        f"WITH {_LATEST_OPS} SELECT teams.team_name AS team, "
        "COUNT(incidents.incident_id) AS incident_count FROM incidents "
        "JOIN services ON incidents.service = services.service "
        f"JOIN teams ON services.team_id = teams.team_id {_IN_LATEST_OPS} "
        "GROUP BY teams.team_name ORDER BY incident_count DESC LIMIT 1", ["incident_count"]),
    "mean time to restore by service tier last month": (
        f"WITH {_LATEST_OPS} SELECT services.tier AS tier, "
        "ROUND(AVG(incidents.minutes_to_restore) / 60.0, 3) AS mttr_hours FROM incidents "
        f"JOIN services ON incidents.service = services.service {_IN_LATEST_OPS} "
        "GROUP BY services.tier ORDER BY tier", ["mttr_hours"]),
}


def question_key(question: str) -> str:
    return " ".join(question.lower().strip().rstrip("?").split())


def question_from(messages: list[dict]) -> str:
    """The question the SQL-writer prompt carries on the line after ``QUESTION:``."""
    from agentic_reporting.sql_writer import QUESTION_MARKER
    for m in reversed(messages):          # a repair turn follows the original question
        lines = str(m.get("content", "")).splitlines()
        for n, line in enumerate(lines):
            if line.strip() == QUESTION_MARKER and n + 1 < len(lines):
                return lines[n + 1]
    raise ValueError("no QUESTION block in the prompt")


class FakeSQLProvider:
    """Offline SQL writer: returns the hand-written SQL for a golden question.

    It reads the question from the prompt exactly as a real model would. Any other question
    raises ``ProviderUnavailable``: the fake proves the pipeline (link, guard, read-only
    execution, comparison), not a model's SQL.
    """

    name = "fake"
    model = "sql-fake-1"

    def __init__(self, extra: dict[str, tuple[str, list[str]]] | None = None) -> None:
        """``extra``: more hand-written answers, keyed like ``GOLDEN_SQL`` (the chat goldens)."""
        self.calls: list[dict[str, Any]] = []
        self.known = {**GOLDEN_SQL, **(extra or {})}

    def complete(self, *, system: str, messages: list[dict], max_tokens: int,
                 schema: Any = None, cache_system: bool = True) -> LLMResponse:
        from agentic_reporting.providers.base import ProviderUnavailable
        from agentic_reporting.sql_writer import SQLAnswer

        self.calls.append({"system": system, "messages": messages})
        question = question_from(messages)
        if question_key(question) not in self.known:
            raise ProviderUnavailable(
                "the offline fake SQL writer knows only the golden questions in "
                "eval/datasets/golden/schema_questions_*.json; for any other question use "
                "--provider anthropic|bedrock|ollama")
        sql, metrics = self.known[question_key(question)]
        answer = SQLAnswer(sql=sql, uses_metrics=metrics,
                           rationale="hand-written SQL for a golden question (offline fake)")
        in_tok = (len(system) + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        return LLMResponse(text=answer.model_dump_json(), parsed=answer, provider=self.name,
                           model=self.model, input_tokens=in_tok, output_tokens=len(sql) // 4,
                           cost_usd=0.0, structured_mode="native")


class FakeCypherProvider:
    """Offline Cypher writer: returns the hand-written Cypher for a golden graph question.

    ``known``: question → Cypher (``eval/datasets/golden/cypher_questions_adventureworks.json``).
    Any other question raises ``ProviderUnavailable``. Like ``FakeSQLProvider``, it proves the
    pipeline (name resolution, guard, scoping, read-only execution, comparison), not a model.
    """

    name = "fake"
    model = "cypher-fake-1"

    def __init__(self, known: dict[str, str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.known = {question_key(q): c for q, c in known.items()}

    def complete(self, *, system: str, messages: list[dict], max_tokens: int,
                 schema: Any = None, cache_system: bool = True) -> LLMResponse:
        from agentic_reporting.providers.base import ProviderUnavailable
        from agentic_reporting.text_to_cypher import CypherAnswer

        self.calls.append({"system": system, "messages": messages})
        key = question_key(question_from(messages))
        if key not in self.known:
            raise ProviderUnavailable("the offline fake Cypher writer knows only the golden graph "
                                      "questions; for any other question use --provider bedrock-converse")
        answer = CypherAnswer(cypher=self.known[key],
                              rationale="hand-written Cypher for a golden question (offline fake)")
        in_tok = (len(system) + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        return LLMResponse(text=answer.model_dump_json(), parsed=answer, provider=self.name,
                           model=self.model, input_tokens=in_tok, output_tokens=len(answer.cypher) // 4,
                           cost_usd=0.0, structured_mode="native")
