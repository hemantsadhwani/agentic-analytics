"""Text-to-Cypher eval over the AdventureWorks knowledge graph in Neo4j: answers checked against SQL.

    python -m eval.cypher_eval --fake                                  # offline writer, $0
    python -m eval.cypher_eval --provider bedrock-converse --model google.gemma-3-12b-it \\
        --fallback-model deepseek.v3.2 --out eval/results/cypher_bench_<date>.json   # real calls

Needs Neo4j (env ``NEO4J_URI``, ``NEO4J_USER``, ``NEO4J_PASSWORD``) and
``samples/adventureworks.sqlite``. It loads the graph first (idempotent; ``graphdb.load``).

Golden file: ``eval/datasets/golden/cypher_questions_adventureworks.json``. The expected answer of
every question is computed here by its ``reference_sql`` over the warehouse, never stored. Per
question: the governed path (``text_to_cypher.ask``) writes Cypher, the guard checks it, and it
runs in a read transaction; the first column is compared with the SQL answer (``set``: the same
distinct values; ``count``: the same number; ``top``: the same first value, and the SQL must have
no tie; ``write``: the graph must be unchanged afterwards, refused or not).

Fallback: when the primary model's Cypher is refused (after its repair) or fails in Neo4j twice,
the fallback model gets the question once. A wrong but valid answer is not retried: at run time
nobody knows it is wrong.

Negative controls (``--fake``, every run) prove the gates can fail:

* ``unsafe``: every answer is a write (``... DETACH DELETE``): all must be refused before Neo4j;
* ``drops-name``: every named value replaced by another spelling: all must be refused by the
  missing-name check (after its repair);
* ``wrong``: every answer returns a fixed wrong name: execution accuracy must be zero.

``--fake`` uses the hand-written Cypher (``FakeCypherProvider``): it proves resolution, the guard,
scoping, read-only execution and the comparison, not a model. Exit 1 on any failure with ``--fake``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "datasets" / "golden" / "cypher_questions_adventureworks.json"
DB = ROOT / "samples" / "adventureworks.sqlite"


def load_golden(path: Path = GOLDEN) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


def expected(db: Path, q: dict[str, Any]) -> Any:
    if q["kind"] == "write":
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(q["reference_sql"]).fetchall()
    finally:
        conn.close()
    if q["kind"] == "count":
        return int(rows[0][0])
    if q["kind"] == "top":
        if len(rows) > 1 and rows[0][1] == rows[1][1]:
            raise ValueError(f"{q['id']}: the reference answer is a tie: {rows}")
        return rows[0][0]
    return sorted({str(r[0]) for r in rows})


def score(kind: str, exp: Any, rows: list[list[Any]]) -> bool:
    if kind == "set":
        return sorted({str(r[0]) for r in rows if r}) == exp
    if kind == "count":
        return len(rows) == 1 and isinstance(rows[0][0], int) and rows[0][0] == exp
    if kind == "top":
        return bool(rows) and rows[0][0] == exp
    raise ValueError(kind)


class _Transform:
    """Wraps a provider and rewrites its Cypher (the negative controls)."""

    def __init__(self, inner: Any, fn: Any) -> None:
        self.inner, self.fn = inner, fn
        self.name, self.model = inner.name, inner.model + "+control"

    def complete(self, **kw: Any) -> Any:
        from agentic_reporting.text_to_cypher import CypherAnswer

        resp = self.inner.complete(**kw)
        a = CypherAnswer(cypher=self.fn(resp.parsed.cypher), rationale="control")
        return resp.model_copy(update={"parsed": a, "text": a.model_dump_json()})


def run(provider: Any, fallback: Any | None, questions: list[dict[str, Any]], ctx: dict[str, Any],
        verbose: bool = True) -> dict[str, Any]:
    from neo4j.exceptions import ClientError

    from agentic_reporting import text_to_cypher as t2c
    from agentic_reporting.providers.base import ProviderUnavailable

    rows_out, cost = [], 0.0
    driver, schema, ns = ctx["driver"], ctx["schema"], ctx["ns"]
    for q in questions:
        exp = expected(ctx["db"], q)
        before = ctx["kg"].counts() if q["kind"] == "write" else None
        rec: dict[str, Any] = {"id": q["id"], "question": q["question"], "kind": q["kind"],
                               "split": q.get("split", "dev")}
        attempts = [provider] + ([fallback] if fallback is not None else [])
        for n, p in enumerate(attempts):
            t0 = time.perf_counter()
            try:
                w, res = t2c.ask(q["question"], p, driver, schema, ctx["idx"], ctx["spec"], ns)
                cost += w.cost_usd
                rec.update(status="executed", model=w.model, cypher=w.cypher, repaired=w.repaired,
                           repair_reasons=w.repair_reasons, rows=res.rows, truncated=res.truncated,
                           cost_usd=w.cost_usd, fallback_used=n > 0)
                break
            except (t2c.CypherRefused, ClientError, ProviderUnavailable, ValueError) as e:
                rec.update(status="refused", model=getattr(p, "model", "?"), error=str(e)[:400],
                           fallback_used=n > 0)
            finally:
                rec["seconds"] = round(time.perf_counter() - t0, 2)
        if q["kind"] == "write":
            rec["ok"] = ctx["kg"].counts() == before
            rec["guard_refused"] = rec["status"] == "refused"
        else:
            rec["expected"] = exp
            rec["ok"] = rec["status"] == "executed" and score(q["kind"], exp, rec["rows"])
        if "rows" in rec and len(rec["rows"]) > 25:
            rec["rows"] = rec["rows"][:25] + [["... (cut in this report)"]]
        rows_out.append(rec)
        if verbose:
            print(f"  {q['id']} {'PASS' if rec['ok'] else 'FAIL'} [{rec['status']}"
                  f"{', fallback' if rec.get('fallback_used') else ''}] {q['question']}")
            if not rec["ok"]:
                print(f"      cypher: {rec.get('cypher') or rec.get('error')}")
                print(f"      expected: {exp}  got: {rec.get('rows')}")
    passed = sum(r["ok"] for r in rows_out)
    splits = {sp: {"passed": sum(r["ok"] for r in rows_out if r["split"] == sp),
                   "total": sum(r["split"] == sp for r in rows_out),
                   "primary_only": sum(r["ok"] and not r.get("fallback_used") for r in rows_out
                                       if r["split"] == sp)}
              for sp in sorted({r["split"] for r in rows_out})}
    return {"passed": passed, "total": len(rows_out), "splits": splits, "cost_usd": round(cost, 6),
            "questions": rows_out}


def controls(fake: Any, questions: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    import re

    reads = [q for q in questions if q["kind"] != "write"]

    def unsafe(c: str) -> str:
        return re.sub(r"\bRETURN\b.*$", "DETACH DELETE p RETURN 1", c, flags=re.S)

    def drops(c: str) -> str:
        return re.sub(r"'([^']*)'", lambda m: "'" + m.group(1).upper() + "'", c)

    def wrong(c: str) -> str:
        return re.sub(r"\bRETURN\b.*$", "RETURN 'not-the-answer' AS v LIMIT 1", c, flags=re.S)

    out = {}
    for name, fn in (("unsafe", unsafe), ("drops-name", drops), ("wrong", wrong)):
        before = ctx["kg"].counts()
        r = run(_Transform(fake, fn), None, reads, ctx, verbose=False)
        refused = sum(x["status"] == "refused" for x in r["questions"])
        named = sum(bool(re.search(r"'[^']+'", q["fake_cypher"])) for q in reads)
        ok = {"unsafe": refused == len(reads), "drops-name": refused == named,
              "wrong": r["passed"] == 0 and refused == 0}[name] and ctx["kg"].counts() == before
        out[name] = {"ok": ok, "refused": refused, "passed": r["passed"], "of": len(reads)}
        print(f"  control {name}: {'PASS' if ok else 'FAIL'} (refused {refused}/{len(reads)}, "
              f"answered right {r['passed']})")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fake", action="store_true", help="the offline hand-written Cypher writer")
    ap.add_argument("--provider", default="bedrock-converse")
    ap.add_argument("--model", default=None)
    ap.add_argument("--fallback-model", default=None, help="tried once when the primary is refused")
    ap.add_argument("--ns", default=None, help="namespace label (default AgenticKG)")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--out", default=None, help="write the result JSON here")
    args = ap.parse_args(argv)

    from agentic_reporting import graphdb
    if graphdb.env_config() is None:
        print("cypher eval needs Neo4j: set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD")
        return 2
    db = Path(args.db)
    if not db.is_file():
        print(f"{db} not found: python samples/load_adventureworks.py")
        return 2
    from agentic_reporting import ontology as onto
    from agentic_reporting.providers.fake import FakeCypherProvider
    from agentic_reporting.providers.router import get_provider
    from agentic_reporting.schema_graph import build_schema_graph
    from agentic_reporting.values import build_value_index

    ns = args.ns or graphdb.DEFAULT_NS
    questions = load_golden()
    driver = graphdb.connect()
    try:
        loaded = graphdb.load(driver, graphdb.build_lpg(db), ns)
        kg = graphdb.CypherKG(driver, ns)
        schema = replace(kg.schema, present=frozenset(kg.counts()[0]))     # prompt: labels with nodes
        ctx = {"driver": driver, "schema": schema, "ns": ns, "db": db, "spec": onto.load_spec(),
               "idx": build_value_index(db, build_schema_graph(db)), "kg": kg}
        print(f"graph :{ns}: {loaded['nodes']} nodes, {loaded['relationships']} relationships")
        fake = FakeCypherProvider({q["question"]: q["fake_cypher"] for q in questions})
        if args.fake:
            provider, fallback = fake, None
        else:
            provider = get_provider(args.provider, args.model)
            fallback = get_provider(args.provider, args.fallback_model) if args.fallback_model else None
        print(f"writer: {provider.name}/{provider.model}"
              + (f", fallback {fallback.model}" if fallback else ""))
        result = run(provider, fallback, questions, ctx)
        result["controls"] = controls(fake, questions, ctx)
    finally:
        driver.close()
    result.update(provider=provider.name, model=provider.model,
                  fallback_model=fallback.model if fallback else None,
                  date=time.strftime("%Y-%m-%d"), graph=loaded)
    print(f"text-to-Cypher: {result['passed']}/{result['total']} right, cost ${result['cost_usd']:.6f}; "
          + ", ".join(f"{k} {v['passed']}/{v['total']} ({v['primary_only']} without the fallback)"
                      for k, v in result["splits"].items()))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8",
                                  newline="\n")
        print(f"wrote {args.out}")
    controls_ok = all(c["ok"] for c in result["controls"].values())
    if args.fake:
        return 0 if result["passed"] == result["total"] and controls_ok else 1
    return 0 if controls_ok else 1


if __name__ == "__main__":
    sys.exit(main())
