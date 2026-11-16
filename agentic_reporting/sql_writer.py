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
2. every name in ``uses_metrics`` is a metric the link returned;
3. the SQL applies what was resolved in code before the model was called: every value filter
   (``'Mountain-200 Black, 38'``) and both dates of the time range appear in it as literals.
   A miss gets one repair round-trip that names what is missing, then it is refused. This
   catches the silent failure where the model drops a filter and answers a bigger question.

``execute`` then runs it through ``data.run_query``: read-only URI, ``PRAGMA query_only``,
the 200-row cap, and a SQLite authorizer that lets it read only the linked tables.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentic_reporting.data import QueryResult, run_query
from agentic_reporting.providers.base import Provider
from agentic_reporting.schema_graph import LinkResult
from agentic_reporting.spec import UnsafeSQL, validate_sql

SQL_MAX_TOKENS = 700
MIN_CONFIDENCE = 0.5
QUESTION_MARKER = "QUESTION:"
SUBSET_MARKER = "LINKED_SCHEMA_JSON:"
REPAIR_MARKER = "REPAIR:"

SYSTEM_PROMPT = """You write one SQLite query that answers a business question.

You see only part of the schema: the tables, columns, join path and governed metrics that
schema linking selected for this question. Rules, in order of importance:
1. Use only the listed tables and columns. Join only along the listed join path.
2. For a governed metric, write its expression in full, exactly as given, then AS and the
   metric's name: ROUND(SUM(orders.revenue), 2) AS net_revenue. The metric's name is not
   a column. Never redefine a metric. List every metric you used in uses_metrics by its
   name only, for example ["net_revenue"].
3. If time_range is given, filter the metric's time column with exactly
   <time_column> BETWEEN '<start>' AND '<end>' (dates are 'YYYY-MM-DD' text). Otherwise
   "last month" or "latest month" means the latest calendar month present in the data:
   MAX(substr(<time_column>, 1, 7)) over the metric's time column.
4. If filters are given, apply every one exactly as written: <column> = '<value>', or
   <column> IN ('<v1>', '<v2>') for several values. Never change the spelling of a value.
5. One statement: SELECT, or WITH ... SELECT. No semicolons, no comments, no writes.
6. Return few columns: the dimension the question asks about, then the metric value.
   For "which ... most/highest", ORDER BY the metric DESC LIMIT 1; for "top N", LIMIT N.
   "By month" groups by substr(<time_column>, 1, 7).

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
    input_tokens: int = 0
    output_tokens: int = 0
    repaired: bool = False
    repair_reasons: list[str] = field(default_factory=list)
    first_sql: str | None = None


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def metric_name(entry: str, link_result: LinkResult) -> str:
    """A ``uses_metrics`` entry as a metric name. Some models list the expression instead
    ("ROUND(SUM(x), 2) AS internet_sales"); only an exact linked expression is mapped back."""
    e = " ".join(entry.split())
    for name, d in link_result.metric_definitions.items():
        expr = " ".join(d["expression"].split())
        if e in (name, expr, f"{expr} AS {name}"):
            return name
    return entry


def missing_requirements(sql: str, link_result: LinkResult) -> list[str]:
    """Resolved filters and dates the SQL does not contain as literals."""
    missing = []
    for f in link_result.filters:
        for v in f["values"]:
            if sql_literal(v) not in sql:
                missing.append(f"filter {f['column']} = {sql_literal(v)}")
    tr = link_result.time_range
    if tr:
        for k in ("start", "end"):
            if sql_literal(tr[k]) not in sql:
                missing.append(f"time range {k} {sql_literal(tr[k])} on {tr['column']}")
    return missing


def user_message(question: str, link_result: LinkResult) -> str:
    return "\n".join([
        QUESTION_MARKER,
        question.strip(),
        "",
        SUBSET_MARKER,
        json.dumps(link_result.subset(), sort_keys=True),
    ])


def _ask(provider: Provider, messages: list[dict]) -> tuple[SQLAnswer, Any]:
    resp = provider.complete(system=SYSTEM_PROMPT, messages=messages,
                             max_tokens=SQL_MAX_TOKENS, schema=SQLAnswer)
    answer = (resp.parsed if isinstance(resp.parsed, SQLAnswer)
              else SQLAnswer.model_validate_json(resp.text))
    return answer, resp


def write_sql(question: str, link_result: LinkResult, provider: Provider,
              error: tuple[str, str] | None = None) -> WrittenSQL:
    """One structured provider call over the linked subset (plus at most one repair when
    the SQL misses a resolved filter or date); raises ``SQLRefused``.

    ``error=(sql, message)``: the previous SQL failed in SQLite; the model sees the error once.
    """
    if not link_result.tables:
        raise SQLRefused("schema linking found no table for this question; no SQL written")
    messages = [{"role": "user", "content": user_message(question, link_result)}]
    if error:
        messages += [{"role": "assistant", "content": json.dumps({"sql": error[0]})},
                     {"role": "user", "content": f"{REPAIR_MARKER} SQLite rejected that SQL: "
                      f"{error[1][:300]}. Rewrite it, using only the listed tables and columns, "
                      "and return the JSON object only."}]
    answer, resp = _ask(provider, messages)
    cost, tin, tout, repaired = resp.cost_usd, resp.input_tokens, resp.output_tokens, False
    missing = missing_requirements(answer.sql, link_result)
    reasons, first = list(missing), answer.sql
    if missing:
        repaired = True
        messages += [{"role": "assistant", "content": answer.model_dump_json()},
                     {"role": "user", "content": REPAIR_MARKER + " your SQL does not apply: "
                      + "; ".join(missing) + ". Rewrite it so it applies every one exactly, "
                      "and return the JSON object only."}]
        answer, resp = _ask(provider, messages)
        cost, tin, tout = cost + resp.cost_usd, tin + resp.input_tokens, tout + resp.output_tokens
        missing = missing_requirements(answer.sql, link_result)
    try:
        sql = validate_sql(answer.sql)
    except UnsafeSQL as e:
        raise SQLRefused(f"unsafe SQL from {resp.provider}/{resp.model}: {e}") from e
    used = [metric_name(u, link_result) for u in answer.uses_metrics]
    unknown = sorted(set(used) - set(link_result.metrics))
    if unknown:
        raise SQLRefused(f"uses_metrics names metric(s) the link did not return: {unknown}")
    if missing:
        raise SQLRefused(f"the SQL does not apply {'; '.join(missing)} (after one repair)")
    return WrittenSQL(sql=sql, uses_metrics=used, rationale=answer.rationale,
                      provider=resp.provider, model=resp.model, cost_usd=cost,
                      input_tokens=tin, output_tokens=tout, repaired=repaired,
                      repair_reasons=reasons, first_sql=first if repaired else None)


def write_and_execute(question: str, link_result: LinkResult, provider: Provider,
                      db_path: str | Path) -> tuple[WrittenSQL, QueryResult]:
    """``write_sql`` then ``execute``; a SQLite error (bad column, bad syntax) gets one repair
    turn that shows the model the error, then the question is refused."""
    written = write_sql(question, link_result, provider)
    try:
        return written, execute(db_path, written, link_result)
    except sqlite3.DatabaseError as e:
        err = (written.sql, str(e))
    again = write_sql(question, link_result, provider, error=err)
    again.cost_usd += written.cost_usd
    again.input_tokens += written.input_tokens
    again.output_tokens += written.output_tokens
    again.repaired = True
    again.repair_reasons = [*written.repair_reasons, f"sqlite: {err[1][:120]}", *again.repair_reasons]
    again.first_sql = written.first_sql or written.sql
    try:
        return again, execute(db_path, again, link_result)
    except sqlite3.DatabaseError as e:
        raise SQLRefused(f"the SQL failed in SQLite twice: {e}") from e


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
