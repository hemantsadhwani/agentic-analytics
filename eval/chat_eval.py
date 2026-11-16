"""Chatbot eval over AdventureWorks: understanding, SQL execution accuracy, checked answers.

    python -m eval.chat_eval --fake                                   # offline gate, $0
    python -m eval.chat_eval --provider bedrock-converse --model google.gemma-3-12b-it \\
        --repeats 3 --out eval/results/chat_bench_<date>.json          # real calls, costs cents

Golden file: ``eval/datasets/golden/chat_questions_adventureworks.json`` (regenerate with
``python -m eval.datasets.golden.make_chat_golden``). The eval first re-runs every
``reference_sql`` and fails if it no longer gives ``expected``. Per question it scores:

* **behaviour**: the status is the expected one (answered / clarify / not_understood /
  refused); a clarify offers the expected metrics;
* **understanding** (answered only, no LLM involved): the governed metric, the linked tables,
  the resolved value filters and the resolved date range are exactly the expected ones;
* **execution accuracy**: the model's SQL returns the expected rows: same row count, and every
  expected cell is in its row (text equal, numbers within 0.5%); extra columns are allowed;
* **answer check**: a model-written answer passed the deterministic number checker (a
  one-row result is answered by template and has nothing to check).

Negative controls (offline, every run) prove the gates can fail:

* ``unsafe``: every SQL answer is an ``UPDATE``: all must be refused before execution;
* ``drops-filter``: the right SQL with every value filter and date removed: every question
  that has a filter or a date must be refused (the missing-literal check, after its repair);
* ``no-rows``: the right SQL returning no rows: execution accuracy must be zero.

``--fake`` writes the golden reference SQL (``FakeSQLProvider``) and template prose
(``FactsFakeProvider``): it proves understanding, the guards and the comparison, not a model's
SQL. A real provider measures the model. Exit 1 on any failure in ``--fake`` mode.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "datasets" / "golden" / "chat_questions_adventureworks.json"
DB = ROOT / "samples" / "adventureworks.sqlite"
TOLERANCE = 0.005


def _cell_eq(a: Any, e: Any) -> bool:
    if isinstance(e, (int, float)) and not isinstance(e, bool):
        return (isinstance(a, (int, float)) and not isinstance(a, bool)
                and math.isclose(float(a), float(e), rel_tol=TOLERANCE, abs_tol=1e-9))
    return a == e


def rows_match(actual: list[list[Any]], expected: list[list[Any]]) -> bool:
    """Same row count; each expected row's cells all appear in a distinct actual row."""
    def empty(rows: list[list[Any]]) -> bool:
        return not rows or all(all(v is None for v in r) for r in rows)
    if empty(expected) or empty(actual):
        return empty(expected) and empty(actual)
    if len(actual) != len(expected):
        return False
    free = list(range(len(actual)))
    for e_row in expected:
        hit = next((i for i in free if all(any(_cell_eq(a, e) for a in actual[i])
                                           for e in e_row)), None)
        if hit is None:
            return False
        free.remove(hit)
    return True


def understood(a: Any, q: dict[str, Any]) -> tuple[bool, list[str]]:
    why = []
    if not a.metric or a.metric["name"] != q["expected_metric"]:
        why.append(f"metric {(a.metric or {}).get('name')} != {q['expected_metric']}")
    if set(a.tables) != set(q["expected_tables"]):
        why.append(f"tables {sorted(a.tables)} != {sorted(q['expected_tables'])}")
    got = {f["column"]: sorted(f["values"]) for f in a.filters}
    want = {k: sorted(v) for k, v in q["expected_filters"].items()}
    if got != want:
        why.append(f"filters {got} != {want}")
    tr = [a.time_range["start"], a.time_range["end"]] if a.time_range else None
    if tr != q["expected_time"]:
        why.append(f"time {tr} != {q['expected_time']}")
    return not why, why


# --- negative-control providers ---------------------------------------------------------

class _Wrap:
    def __init__(self, inner: Any) -> None:
        self.inner, self.name, self.model = inner, inner.name, f"{inner.model}+control"

    def complete(self, **kw: Any) -> Any:
        resp = self.inner.complete(**kw)
        resp.parsed.sql = self.alter(resp.parsed.sql)
        return resp


class UnsafeSQL(_Wrap):
    def alter(self, sql: str) -> str:
        return "UPDATE FactInternetSales SET SalesAmount = 0"


class DropsFilters(_Wrap):
    """Removes every literal the resolver required: the model 'forgot' the filter."""
    def alter(self, sql: str) -> str:
        sql = re.sub(r"\s+AND\s+[\w.]+\s*(?:=\s*'[^']*'|IN\s*\([^)]*\))", "", sql)
        sql = re.sub(r"WHERE\s+[\w.]+\s*(?:=\s*'[^']*'|IN\s*\([^)]*\))\s+AND\s+", "WHERE ", sql)
        sql = re.sub(r"\s+(?:AND\s+)?[\w.]+\s+BETWEEN\s+'[^']*'\s+AND\s+'[^']*'", "", sql)
        return re.sub(r"WHERE\s+(GROUP|ORDER|LIMIT|$)", r"\1", sql).strip()


class NoRows(_Wrap):
    def alter(self, sql: str) -> str:
        return f"SELECT * FROM ({sql}) LIMIT 0"


# --- run ---------------------------------------------------------------------------------

def run_one(q: dict[str, Any], sql_p: Any, ans_p: Any) -> dict[str, Any]:
    from agentic_reporting.chat import ask
    a = ask(q["question"], DB, sql_p, ans_p, metric=q.get("metric"))
    r: dict[str, Any] = {"question": q["question"], "metric_choice": q.get("metric"),
                         "status": a.status, "expected_status": q["expected_status"],
                         "behaved": a.status == q["expected_status"], "text": a.text,
                         "sql": a.sql, "rows": a.rows, "answer_mode": a.answer_mode,
                         "check_ok": (a.check or {}).get("ok"), "sql_repaired": a.sql_repaired,
                         "repair_reasons": a.repair_reasons, "first_sql": a.first_sql,
                         "input_tokens": a.input_tokens, "output_tokens": a.output_tokens,
                         "cost_usd": a.cost_usd, "latency_ms": a.latency_ms}
    if q["expected_status"] == "clarify":
        r["behaved"] &= sorted(o.get("metric") for o in a.options) == sorted(q["expected_options"])
    if q["expected_status"] == "answered":
        ok, why = understood(a, q) if a.metric else (False, ["no metric"])
        r["understood"], r["understood_why"] = ok, why
        r["correct"] = a.status == "answered" and rows_match(a.rows, q["expected"])
    return r


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    ans = [r for r in results if r["expected_status"] == "answered"]
    mod = [r for r in ans if r["answer_mode"] == "model"]
    n = len(results)
    return {
        "questions": n, "behaved": sum(r["behaved"] for r in results),
        "answered_questions": len(ans),
        "understood": sum(r.get("understood", False) for r in ans),
        "execution_correct": sum(r.get("correct", False) for r in ans),
        "sql_repaired": sum(r["sql_repaired"] for r in ans),
        "model_answers": len(mod), "model_answers_checked_ok": sum(bool(r["check_ok"]) for r in mod),
        "input_tokens": sum(r["input_tokens"] for r in results),
        "output_tokens": sum(r["output_tokens"] for r in results),
        "cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "cost_per_answered_usd": round(sum(r["cost_usd"] for r in ans) / max(1, len(ans)), 6),
        "p50_latency_ms": sorted(r["latency_ms"] for r in ans)[len(ans) // 2] if ans else 0,
    }


def controls(golden: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    from agentic_reporting.chat import golden_sql
    from agentic_reporting.providers.fake import FactsFakeProvider, FakeSQLProvider
    fails, lines = [], []
    ans = [q for q in golden if q["expected_status"] == "answered"]
    fake = FakeSQLProvider(extra=golden_sql(DB))
    u = [run_one(q, UnsafeSQL(fake), FactsFakeProvider()) for q in ans]
    refused = sum(r["status"] == "refused" for r in u)
    lines.append(f"control/unsafe        {refused}/{len(ans)} UPDATE answers refused before execution")
    if refused != len(ans):
        fails.append("control/unsafe: an UPDATE was not refused")
    needs = [q for q in ans if q["expected_filters"] or q["expected_time"]]
    d = [run_one(q, DropsFilters(fake), FactsFakeProvider()) for q in needs]
    refused = sum(r["status"] == "refused" for r in d)
    lines.append(f"control/drops-filter  {refused}/{len(needs)} answers with the filter or dates "
                 "removed refused (after one repair)")
    if refused != len(needs):
        fails.append("control/drops-filter: SQL without its filter was executed")
    nr = [run_one(q, NoRows(fake), FactsFakeProvider()) for q in ans]
    nonempty = [q for q, r in zip(ans, nr, strict=True)
                if r.get("correct") and q["expected"] and q["expected"] != [[None]]]
    lines.append(f"control/no-rows       {len(nonempty)} non-empty answers matched when the SQL "
                 "returns no rows")
    if nonempty:
        fails.append("control/no-rows: the comparison is vacuous")
    return fails, lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fake", action="store_true", help="offline: reference SQL + template prose")
    ap.add_argument("--provider", default="bedrock-converse")
    ap.add_argument("--model", help="LLM_MODEL for the provider (e.g. google.gemma-3-12b-it)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", help="write the full results as JSON")
    args = ap.parse_args(argv)

    from agentic_reporting.data import run_query
    if not DB.is_file():
        print(f"CHAT-EVAL FAIL: {DB} is missing; build it with python samples/load_adventureworks.py")
        return 1
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["questions"]
    failures: list[str] = []
    for q in golden:
        if q["expected_status"] == "answered":
            ref = run_query(DB, q["reference_sql"]).rows
            if not rows_match(ref, q["expected"]):
                failures.append(f"reference_sql drifted for {q['question']!r}: {ref}")

    if args.fake:
        from agentic_reporting.chat import golden_sql
        from agentic_reporting.providers.fake import FactsFakeProvider, FakeSQLProvider
        sql_p, ans_p = FakeSQLProvider(extra=golden_sql(DB)), FactsFakeProvider()
        label = "fake/reference-sql"
    else:
        import os
        if args.model:
            os.environ["LLM_MODEL"] = args.model
        from agentic_reporting.providers.router import get_provider
        sql_p = ans_p = get_provider(args.provider)
        label = f"{args.provider}/{getattr(sql_p, 'model', '?')}"

    runs = []
    t0 = time.time()
    for rep in range(args.repeats):
        results = [run_one(q, sql_p, ans_p) for q in golden]
        runs.append(results)
        for r in results:
            tag = "ok  " if r["behaved"] and r.get("understood", True) and r.get("correct", True) \
                else "FAIL"
            extra = ""
            if r["expected_status"] == "answered":
                extra = (f" und {'ok' if r['understood'] else 'MISS'} exec "
                         f"{'ok' if r['correct'] else 'MISS'} {r['answer_mode']}"
                         + (f" check {'ok' if r['check_ok'] else 'FAIL'}"
                            if r["answer_mode"] == "model" else ""))
            print(f"  {tag} r{rep + 1} {r['status']:<14}{extra:<34} {r['question'][:64]}")
            if tag == "FAIL":
                why = "; ".join(r.get("understood_why") or []) or r["text"][:150]
                print(f"         {why}")
                if r["sql"] and not r.get("correct", True):
                    print(f"         sql: {r['sql'][:300]}")
                    print(f"         got: {str(r['rows'])[:200]}")
    s = [summarise(r) for r in runs]
    total = {k: sum(x[k] for x in s) for k in s[0] if k not in ("p50_latency_ms",
                                                                 "cost_per_answered_usd")}
    total["cost_per_answered_usd"] = round(sum(x["cost_per_answered_usd"] for x in s) / len(s), 6)
    total["p50_latency_ms"] = sorted(x["p50_latency_ms"] for x in s)[len(s) // 2]
    print(f"{label} x{args.repeats}: behaviour {total['behaved']}/{total['questions']} · "
          f"understanding {total['understood']}/{total['answered_questions']} · execution "
          f"{total['execution_correct']}/{total['answered_questions']} · model answers checked "
          f"{total['model_answers_checked_ok']}/{total['model_answers']} · SQL repaired "
          f"{total['sql_repaired']} · ${total['cost_usd']:.5f} total, "
          f"${total['cost_per_answered_usd']:.6f}/answered question · p50 "
          f"{total['p50_latency_ms']} ms · {time.time() - t0:.0f}s")

    cfails, clines = controls(golden)
    for line in clines:
        print(f"  {'ok  ' if not cfails else '    '} {line}")
    failures += cfails
    if args.fake:
        if total["behaved"] != total["questions"]:
            failures.append("behaviour misses")
        if total["understood"] != total["answered_questions"]:
            failures.append("understanding misses")
        if total["execution_correct"] != total["answered_questions"]:
            failures.append("execution misses")
    if args.out:
        Path(args.out).write_text(json.dumps({"provider": label, "repeats": args.repeats,
                                              "summary": total, "runs": runs}, indent=1,
                                             default=str) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    for f in failures:
        print(f"  - {f}")
    if failures and args.fake:
        print(f"CHAT-EVAL FAIL ({len(failures)})")
        return 1
    print("CHAT-EVAL " + ("PASS" if not failures else "DONE (real model: misses are measurements)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
