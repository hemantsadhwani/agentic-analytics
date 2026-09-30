"""Schema-linking eval: question → linked subset → SQL → rows, against golden answers.

    python -m eval.schema_linking_eval --fake

For every ``eval/datasets/golden/schema_questions_*.json`` it builds the SYNTHETIC sample
database in a temp dir and its schema graph, then for each question:

* **linking accuracy**: ``link()`` returned exactly the expected tables, and the expected
  governed metric as its primary metric;
* **execution accuracy**: the provider's SQL, after the guard and the read-only execution
  (linked tables only), returns the expected result: same rows (order-insensitive), text
  cells equal, numbers within 0.5%.

It also re-runs each file's ``reference_sql`` (which hard-codes the month) and fails if it
no longer reproduces ``expected``, so the golden answers cannot silently drift from the data.

Two negative controls prove the gates can fail:

* ``unsafe``: a provider that answers every question with an ``UPDATE``; every answer must
  be refused before execution;
* ``no-rows``: the fake's SQL wrapped to return no rows; execution accuracy must be zero.

``--fake`` uses ``FakeSQLProvider`` (hand-written SQL): it proves linking, the guard, the
read-only path and the comparison, not a model's SQL. Exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GOLDEN_DIR = Path(__file__).resolve().parent / "datasets" / "golden"
TOLERANCE = 0.005


def load_golden(path: Path) -> dict[str, Any]:
    g = json.loads(path.read_text(encoding="utf-8"))
    try:
        assert g["sample"] in ("sales", "ops") and g["questions"]
        for q in g["questions"]:
            assert isinstance(q["question"], str) and isinstance(q["expected_metric"], str)
            assert all(isinstance(t, str) for t in q["expected_tables"])
            assert isinstance(q["reference_sql"], str) and isinstance(q["expected"], list)
    except (AssertionError, KeyError, TypeError) as e:
        raise ValueError(f"{path.name}: malformed golden file ({e!r})") from e
    return g


def _key(row: list[Any]) -> tuple:
    return tuple((0, str(v)) if isinstance(v, str) or v is None else (1, float(v)) for v in row)


def results_match(actual: list[list[Any]], expected: list[list[Any]], tol: float = TOLERANCE) -> bool:
    """Same rows in any order; text equal; numbers within ``tol`` relative (exact at zero)."""
    if len(actual) != len(expected):
        return False
    for a_row, e_row in zip(sorted(actual, key=_key), sorted(expected, key=_key), strict=True):
        if len(a_row) != len(e_row):
            return False
        for a, e in zip(a_row, e_row, strict=True):
            if isinstance(e, (int, float)) and not isinstance(e, bool):
                if not isinstance(a, (int, float)) or isinstance(a, bool):
                    return False
                if not math.isclose(float(a), float(e), rel_tol=tol, abs_tol=0.0 if e else 1e-9):
                    return False
            elif a != e:
                return False
    return True


class UnsafeSQLProvider:
    """Negative control: answers every question with a write."""

    name = "fake"
    model = "unsafe-sql-fake-1"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system, messages, max_tokens, schema=None, cache_system=True):
        from agentic_reporting.providers.base import LLMResponse
        from agentic_reporting.sql_writer import SQLAnswer

        self.calls.append({"messages": messages})
        answer = SQLAnswer(sql="UPDATE orders SET revenue = 0", uses_metrics=[], rationale="control")
        return LLMResponse(text=answer.model_dump_json(), parsed=answer, provider=self.name,
                           model=self.model, input_tokens=0, output_tokens=0, cost_usd=0.0,
                           structured_mode="native")


class NoRowsSQLProvider:
    """Negative control: the right SQL, wrapped so it returns no rows."""

    name = "fake"
    model = "no-rows-sql-fake-1"

    def __init__(self) -> None:
        from agentic_reporting.providers.fake import FakeSQLProvider
        self.inner = FakeSQLProvider()
        self.calls = self.inner.calls

    def complete(self, **kw):
        resp = self.inner.complete(**kw)
        resp.parsed.sql = f"SELECT * FROM ({resp.parsed.sql}) LIMIT 0"
        return resp


def run_question(q: dict[str, Any], graph: Any, db: Path, provider: Any) -> dict[str, Any]:
    from agentic_reporting.schema_graph import link
    from agentic_reporting.sql_writer import SQLRefused, execute, write_sql

    lr = link(q["question"], graph)
    linked = (set(lr.tables) == set(q["expected_tables"])
              and bool(lr.metrics) and lr.metrics[0] == q["expected_metric"])
    out: dict[str, Any] = {"link": lr, "linked": linked, "refused": None, "rows": None,
                           "executed": False, "correct": False}
    try:
        written = write_sql(q["question"], lr, provider)
        result = execute(db, written, lr)
    except SQLRefused as e:
        out["refused"] = str(e)
        return out
    out["executed"] = True
    out["rows"] = result.rows
    out["correct"] = results_match(result.rows, q["expected"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fake", action="store_true",
                    help="offline FakeSQLProvider only (the only mode today; required)")
    args = ap.parse_args(argv)
    if not args.fake:
        ap.error("pass --fake (a real-provider schema eval is not implemented yet)")

    from agentic_reporting.data import run_query
    from agentic_reporting.providers.fake import FakeSQLProvider
    from agentic_reporting.schema_graph import build_schema_graph
    from samples import make_ops_db, make_sales_db

    goldens = sorted(GOLDEN_DIR.glob("schema_questions_*.json"))
    if not goldens:
        print(f"SCHEMA-EVAL FAIL: no schema_questions_*.json in {GOLDEN_DIR}")
        return 1
    failures: list[str] = []
    n = n_linked = n_correct = 0
    unsafe_refused = unsafe_executed = norows_correct = 0
    provider = FakeSQLProvider()
    with tempfile.TemporaryDirectory(prefix="schema-eval-") as tmp:
        for gpath in goldens:
            golden = load_golden(gpath)
            kind = golden["sample"]
            builder = make_sales_db if kind == "sales" else make_ops_db
            db = builder.build(Path(tmp) / f"{kind}.sqlite")
            graph = build_schema_graph(db)
            for q in golden["questions"]:
                n += 1
                ref = run_query(db, q["reference_sql"]).rows
                if not results_match(ref, q["expected"]):
                    failures.append(f"{kind}: reference_sql no longer gives expected for "
                                    f"{q['question']!r}: {ref}")
                r = run_question(q, graph, db, provider)
                n_linked += r["linked"]
                n_correct += r["correct"]
                lr = r["link"]
                if not r["linked"]:
                    failures.append(f"{kind}: linking miss for {q['question']!r}: tables "
                                    f"{lr.tables} metric {lr.metrics[:1]} (expected "
                                    f"{q['expected_tables']} {q['expected_metric']})")
                if not r["correct"]:
                    failures.append(f"{kind}: execution miss for {q['question']!r}: "
                                    f"{r['refused'] or r['rows']} (expected {q['expected']})")
                ok = r["linked"] and r["correct"]
                rows = r["rows"] or []
                print(f"  {'ok  ' if ok else 'FAIL'} {kind:<5} link {'ok' if r['linked'] else 'MISS'}"
                      f"  exec {'ok' if r['correct'] else 'MISS'}  conf {lr.confidence:.2f}  "
                      f"{q['question']:<62} {'+'.join(lr.tables)} | {(lr.metrics or ['-'])[0]}"
                      f" | {len(rows)} row{'s' if len(rows) != 1 else ''}")
                u = run_question(q, graph, db, UnsafeSQLProvider())
                unsafe_refused += u["refused"] is not None
                unsafe_executed += u["executed"]
                norows_correct += run_question(q, graph, db, NoRowsSQLProvider())["correct"]

    unsafe_ok = unsafe_refused == n and unsafe_executed == 0
    norows_ok = norows_correct == 0
    if not unsafe_ok:
        failures.append(f"control/unsafe: {unsafe_refused}/{n} refused, {unsafe_executed} executed")
    if not norows_ok:
        failures.append(f"control/no-rows: {norows_correct}/{n} matched; the comparison is vacuous")
    print(f"  {'ok  ' if unsafe_ok else 'FAIL'} control/unsafe   {unsafe_refused}/{n} UPDATE answers "
          f"refused before execution, {unsafe_executed} executed")
    print(f"  {'ok  ' if norows_ok else 'FAIL'} control/no-rows  {norows_correct}/{n} execution "
          "matches when the SQL returns no rows")
    print(f"linking accuracy {n_linked}/{n} ({100 * n_linked / n:.0f}%) · "
          f"execution accuracy {n_correct}/{n} ({100 * n_correct / n:.0f}%) · tolerance 0.5%")
    for f in failures:
        print(f"  - {f}")
    if failures:
        print(f"SCHEMA-EVAL FAIL ({len(failures)} failure(s))")
        return 1
    print(f"SCHEMA-EVAL PASS ({len(goldens)} sample DBs, {n} questions, "
          f"provider {provider.name}/{provider.model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
