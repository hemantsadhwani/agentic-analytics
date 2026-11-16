"""RAG eval: the whole corpus in the prompt (A) against hybrid retrieval (B), same checks.

    python -m eval.rag_eval --fake                       # offline gate, $0 (CI)
    python -m eval.rag_eval --provider bedrock-converse --model google.gemma-3-12b-it \\
        --embedder titan --repeats 3 --out eval/results/rag_bench_<date>.json

Golden file: ``eval/datasets/golden/rag_questions_adventureworks.json``. Every question goes
through the chatbot's documents route (``chat.ask`` with ``Documents``), in both modes:

* **A, "all"**: every chunk in the prompt (the baseline: no index, no retrieval);
* **B, "retrieve"**: keyword + vector search, rank fusion, top 5 chunks in the prompt.

Scored per question:

* answerable: **correct** = answered, cites one of ``expected_ids``, mentions every
  ``must_mention`` string (real models only; the fake quotes a first sentence);
* unanswerable: **correct** = "not found";
* **recall@5** (B only): an expected chunk was in the top 5 retrieved;
* tokens, cost (model + query embedding), latency.

Also: a planted chunk that tells the model to ignore its instructions must be quarantined at
ingestion, and the question it targets must come back "not found".

``--fake`` uses the hashing embedder and ``FakeRagProvider`` (word overlap, not reading); it
proves the plumbing, the checks and the routing, and gates CI. Exit 1 on a miss in --fake mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "datasets" / "golden" / "rag_questions_adventureworks.json"
DB = ROOT / "samples" / "adventureworks.sqlite"
K = 5


class _NoSQL:
    """The documents route must never call the SQL writer; this proves it."""
    name, model = "none", "none"

    def complete(self, **kw: Any) -> Any:
        raise AssertionError("a documents question reached the SQL writer")


def score(q: dict[str, Any], a: Any, fake: bool) -> bool:
    if not q["expected_ids"]:
        return a.status == "not_understood"
    cited = {c["id"] for c in a.citations}
    ok = a.status == "answered" and bool(cited & set(q["expected_ids"]))
    if not fake:
        ok &= all(m.lower() in a.text.lower() for m in q.get("must_mention", []))
    return ok


def run_mode(golden: dict[str, Any], mode: str, provider: Any, embedder: Any, fake: bool,
             workdir: Path) -> dict[str, Any]:
    from agentic_reporting.doc_index import DocIndex
    from agentic_reporting.documents import Chunk, load_chunks

    planted = [Chunk(p["id"], "product", p["title"], p["text"]) for p in golden["planted"]]
    chunks, quarantined = load_chunks(DB, extra=planted)
    build: dict[str, Any] = {}
    index = None
    if mode == "retrieve":
        index = DocIndex(workdir / f"rag_eval_{embedder.model_id.replace(':', '_')}.sqlite",
                         embedder)
    try:
        if index is not None:
            t0 = time.perf_counter()
            build = index.sync(chunks)
            build["seconds"] = round(time.perf_counter() - t0, 2)
        rows = _run_questions(golden, chunks, provider, index, fake)
    finally:                          # an open SQLite file cannot be deleted on Windows
        if index is not None:
            index.close()
    if not rows:
        raise RuntimeError("no questions were run")
    return _summary(mode, rows, quarantined, build)


def _run_questions(golden: dict[str, Any], chunks: list[Any], provider: Any, index: Any,
                   fake: bool) -> list[dict[str, Any]]:
    from agentic_reporting.chat import Documents, ask
    docs = Documents(chunks, provider, index, k=K)
    rows = []
    for q in golden["questions"]:
        if fake and q.get("fake_ok") is False:
            continue
        a = ask(q["question"], DB, _NoSQL(), _NoSQL(), documents=docs)
        rec = {"question": q["question"], "status": a.status, "text": a.text,
               "citations": [c["id"] for c in a.citations], "answer_mode": a.answer_mode,
               "correct": score(q, a, fake), "answerable": bool(q["expected_ids"]),
               "input_tokens": a.input_tokens, "output_tokens": a.output_tokens,
               "cost_usd": a.cost_usd, "latency_ms": a.latency_ms}
        if index is not None and q["expected_ids"]:
            got = [h.chunk.id for h in index.retrieve(
                q["question"], k=K, entities=_entities(q["question"], chunks)).hits]
            rec["retrieved"] = got
            rec["recall_at_5"] = bool(set(got) & set(q["expected_ids"]))
        rows.append(rec)
    return rows


def _summary(mode: str, rows: list[dict[str, Any]], quarantined: list[Any],
             build: dict[str, Any]) -> dict[str, Any]:
    n = len(rows)
    ans = [r for r in rows if r["answerable"]]
    lat = sorted(r["latency_ms"] for r in rows)
    return {"mode": mode, "questions": n, "correct": sum(r["correct"] for r in rows),
            "answerable_correct": sum(r["correct"] for r in ans), "answerable": len(ans),
            "unanswerable_correct": sum(r["correct"] for r in rows if not r["answerable"]),
            "unanswerable": n - len(ans),
            "recall_at_5": (sum(r.get("recall_at_5", False) for r in ans) if mode == "retrieve"
                            else None),
            "quarantined": quarantined,
            "input_tokens_per_question": round(sum(r["input_tokens"] for r in rows) / n),
            "output_tokens_per_question": round(sum(r["output_tokens"] for r in rows) / n),
            "cost_per_question_usd": round(sum(r["cost_usd"] for r in rows) / n, 7),
            "p50_latency_ms": lat[n // 2], "p95_latency_ms": lat[min(n - 1, int(n * 0.95))],
            "index_build": build, "rows": rows}


def _entities(question: str, chunks: list[Any]) -> dict[str, list[str]]:
    """The same entity filters the chatbot applies, for the recall measurement."""
    from agentic_reporting.chat import entity_terms, knowledge
    _, idx = knowledge(DB)
    return entity_terms(idx.match(question), chunks)


def _save(out: str | None, label: str, repeats: int, results: list[dict[str, Any]]) -> None:
    if out:
        Path(out).write_text(json.dumps({"provider": label, "repeats": repeats, "k": K,
                                         "results": results}, indent=1, default=str) + "\n",
                             encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--provider", default="bedrock-converse")
    ap.add_argument("--model", help="LLM_MODEL, e.g. google.gemma-3-12b-it")
    ap.add_argument("--embedder", default="titan", help="titan | cohere | cohere-multilingual | fake")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--modes", default="all,retrieve")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if not DB.is_file():
        print(f"RAG-EVAL FAIL: {DB} is missing; build it with python samples/load_adventureworks.py")
        return 1
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    from agentic_reporting.embeddings import get_embedder
    if args.fake:
        from agentic_reporting.rag import FakeRagProvider
        provider: Any = FakeRagProvider()
        embedder = get_embedder("fake")
    else:
        if args.model:
            os.environ["LLM_MODEL"] = args.model
        from agentic_reporting.providers.router import get_provider
        provider = get_provider(args.provider)
        embedder = get_embedder(args.embedder)
    label = (f"{getattr(provider, 'name', '?')}/{getattr(provider, 'model', '?')} + "
             f"{embedder.model_id}")
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="rag-eval-", ignore_cleanup_errors=True) as tmp:
        for rep in range(args.repeats):
            for mode in args.modes.split(","):
                try:
                    r = run_mode(golden, mode, provider, embedder, args.fake, Path(tmp))
                except Exception as e:      # a provider refusal is a result, not a crash
                    msg = f"r{rep + 1} {mode}: run failed: {type(e).__name__}: {str(e)[:200]}"
                    print(f"  FAIL {msg}")
                    failures.append(msg)
                    results.append({"mode": mode, "repeat": rep + 1, "error": msg})
                    _save(args.out, label, args.repeats, results)
                    continue
                r["repeat"] = rep + 1
                results.append(r)
                _save(args.out, label, args.repeats, results)    # every run kept as it ends
                for row in r["rows"]:
                    if not row["correct"] or row.get("recall_at_5") is False:
                        print(f"  MISS r{rep + 1} {mode:<8} {row['status']:<14} "
                              f"{row['question'][:60]:<60} cited {row['citations']} "
                              f"{('top5 ' + str(row['retrieved'][:3])) if 'retrieved' in row else ''}")
                        print(f"       {row['text'][:150]}")
                rec = f" · recall@5 {r['recall_at_5']}/{r['answerable']}" if r["recall_at_5"] \
                    is not None else ""
                b = r["index_build"]
                built = (f" · index: {b['added']} embedded, {b['embed_tokens']} tokens, "
                         f"${b['embed_cost_usd']:.6f}, {b['seconds']}s") if b else ""
                print(f"{label} r{rep + 1} {mode:<8}: correct {r['correct']}/{r['questions']} "
                      f"(answerable {r['answerable_correct']}/{r['answerable']}, not-found "
                      f"{r['unanswerable_correct']}/{r['unanswerable']}){rec} · "
                      f"{r['input_tokens_per_question']} in / {r['output_tokens_per_question']} "
                      f"out tokens per question · ${r['cost_per_question_usd']:.6f}/question · "
                      f"p50 {r['p50_latency_ms']} ms · p95 {r['p95_latency_ms']} ms{built}")
                if len(r["quarantined"]) != len(golden["planted"]):
                    failures.append(f"{mode}: planted injection chunk not quarantined")
                if args.fake:
                    if r["correct"] != r["questions"]:
                        failures.append(f"{mode}: {r['questions'] - r['correct']} wrong")
                    if r["recall_at_5"] is not None and r["recall_at_5"] != r["answerable"]:
                        failures.append(f"{mode}: recall@5 {r['recall_at_5']}/{r['answerable']}")
    print(f"  {'ok  ' if not any('quarantined' in f for f in failures) else 'FAIL'} "
          f"planted injection chunk quarantined at ingestion in every run")
    if args.out:
        print(f"wrote {args.out}")
    for f in failures:
        print(f"  - {f}")
    if failures:
        print(f"RAG-EVAL FAIL ({len(failures)})")
        return 1
    print("RAG-EVAL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
