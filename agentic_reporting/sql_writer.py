"""Question → SQL → rows, with the LLM confined to writing the SQL text.

    g = build_schema_graph(db)
    lr = link(question, g)                         # deterministic, no LLM
    w = write_sql(question, lr, provider)          # ONE structured call, linked subset only
    rows = execute(db, w, lr)                      # read-only, row cap, linked tables only

``write_sql`` shows the model only ``LinkResult.subset()``: the linked tables and columns,
the join path, and the governed metric definitions. It must answer with ``SQLAnswer``
(``sql``, ``uses_metrics``, ``rationale``). The answer is refused (``SQLRefused``) unless:

1. the SQL passes ``spec.validate_sql``: one ``SELECT``/``WITH``, no ``;``, no comments,
   no write or DDL keyword (the same guard as a report spec);
2. every name in ``uses_metrics`` is a metric the link returned.

``execute`` then runs it through ``data.run_query``: read-only URI, ``PRAGMA query_only``,
the 200-row cap, and a SQLite authorizer that lets it read only the linked tables.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from agentic_reporting.data import QueryResult, run_query
from agentic_reporting.providers.base import Provider
from agentic_reporting.schema_graph import LinkResult
from agentic_reporting.spec import UnsafeSQL, validate_sql

SQL_MAX_TOKENS = 700
MIN_CONFIDENCE = 0.5
QUESTION_MARKER = "QUESTION:"
SUBSET_MARKER = "LINKED_SCHEMA_JSON:"

SYSTEM_PROMPT = """You write one SQLite query that answers a business question.

You see only part of the schema: the tables, columns, join path and governed metrics that
schema linking selected for this question. Rules, in order of importance:
1. Use only the listed tables and columns. Join only along the listed join path.
2. For a governed metric, use its expression exactly as written. Never redefine a metric.
   List every metric you used in uses_metrics.
3. "Last month" or "latest month" means the latest calendar month present in the data:
   MAX(substr(<time_column>, 1, 7)) over the metric's time column.
4. One statement: SELECT, or WITH ... SELECT. No semicolons, no comments, no writes.
5. Return few columns: the dimension the question asks about, then the metric value.
   For "which ... most/highest", ORDER BY the metric DESC LIMIT 1.

Respond with ONLY a JSON object: {"sql": "<one SELECT>", "uses_metrics": ["<metric>", ...],
"rationale": "<one sentence>"}"""


class SQLAnswer(BaseModel):
    sql: str = Field(min_length=1)
    uses_metrics: list[str] = Field(default_factory=list)
    rationale: str = ""


class SQLRefused(ValueError):
    """The provider's SQL broke a rule; it was not executed."""


@dataclass
class WrittenSQL:
    sql: str
    uses_metrics: list[str]
    rationale: str
    provider: str
    model: str
    cost_usd: float


def user_message(question: str, link_result: LinkResult) -> str:
    return "\n".join([
        QUESTION_MARKER,
        question.strip(),
        "",
        SUBSET_MARKER,
        json.dumps(link_result.subset(), sort_keys=True),
    ])


def write_sql(question: str, link_result: LinkResult, provider: Provider) -> WrittenSQL:
    """One structured provider call over the linked subset; raises ``SQLRefused``."""
    if not link_result.tables:
        raise SQLRefused("schema linking found no table for this question; no SQL written")
    resp = provider.complete(system=SYSTEM_PROMPT,
                             messages=[{"role": "user", "content": user_message(question, link_result)}],
                             max_tokens=SQL_MAX_TOKENS, schema=SQLAnswer)
    answer = resp.parsed if isinstance(resp.parsed, SQLAnswer) else SQLAnswer.model_validate_json(resp.text)
    try:
        sql = validate_sql(answer.sql)
    except UnsafeSQL as e:
        raise SQLRefused(f"unsafe SQL from {resp.provider}/{resp.model}: {e}") from e
    unknown = sorted(set(answer.uses_metrics) - set(link_result.metrics))
    if unknown:
        raise SQLRefused(f"uses_metrics names metric(s) the link did not return: {unknown}")
    return WrittenSQL(sql=sql, uses_metrics=answer.uses_metrics, rationale=answer.rationale,
                      provider=resp.provider, model=resp.model, cost_usd=resp.cost_usd)


def execute(db_path: str | Path, written: WrittenSQL, link_result: LinkResult) -> QueryResult:
    """Run through the read-only ``data.run_query`` path, reading only the linked tables."""
    try:
        return run_query(db_path, written.sql, allowed_tables=frozenset(link_result.tables))
    except sqlite3.DatabaseError as e:
        # SQLite's wording for an authorizer denial: "access to <t>.<c> is prohibited"
        # for a read, "not authorized" for any other action.
        if "prohibited" in str(e) or "not authorized" in str(e):
            raise SQLRefused(f"SQL reads a table outside the linked subset {link_result.tables}") from e
        raise
