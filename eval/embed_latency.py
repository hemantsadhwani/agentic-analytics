"""Embedding latency on very short queries: Titan V2 against Cohere v3, real Bedrock calls.

    AWS_PROFILE=agentic python -m eval.embed_latency --repeats 5 --out eval/results/embed_latency_<date>.json

What a chatbot pays per question is one query embedding, so this times exactly that: one
``embed([query], kind="query")`` call, measured at the client (network + service), for queries
of 1 to 6 words. Models are called in turn, round-robin, so network drift hits all of them
equally; 3 warm-up calls per model are not counted (TLS setup, connection pool).

Also timed: one batch of 20 queries, because Titan V2 takes one text per call and Cohere v3
takes up to 96. A refusal (e.g. an AWS Marketplace payment error) is recorded, not raised.
Not a CI gate; costs a fraction of a cent.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QUERIES = ["helmet", "road frame", "lightest road frame", "Mountain-200 battery",
           "which saddle is kevlar", "taillight batteries", "bike wash",
           "how many keys cable lock", "hydration pack storage", "tights for winter riding"]
MODELS = {"titan": "amazon.titan-embed-text-v2:0", "cohere": "cohere.embed-english-v3",
          "cohere-multilingual": "cohere.embed-multilingual-v3"}
WARMUP = 3
BATCH = 20


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(len(s) * p))], 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="titan")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    from agentic_reporting.embeddings import BedrockEmbedder
    names = args.models.split(",")
    emb = {n: BedrockEmbedder(MODELS[n]) for n in names}
    times: dict[str, list[float]] = {n: [] for n in names}
    errors: dict[str, str] = {}

    def timed(n: str, texts: list[str]) -> float | None:
        if n in errors:
            return None
        t0 = time.perf_counter()
        try:
            emb[n].embed(texts, kind="query")
        except Exception as e:     # noqa: BLE001  a refusal is a result
            errors[n] = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"  {n}: {errors[n]}")
            return None
        return (time.perf_counter() - t0) * 1000

    for n in names:
        for _ in range(WARMUP):
            timed(n, ["warm up"])
    for _ in range(args.repeats):
        for q in QUERIES:
            for n in names:                       # round-robin: same network moment
                ms = timed(n, [q])
                if ms is not None:
                    times[n].append(ms)
    batch_ms = {n: timed(n, QUERIES * (BATCH // len(QUERIES))) for n in names}

    out: dict[str, Any] = {"date": date.today().isoformat(), "region": "ap-south-1",
                           "queries": QUERIES, "repeats": args.repeats, "warmup": WARMUP,
                           "results": {}}
    print(f"{'model':<34} {'calls':>5} {'p50 ms':>7} {'p95 ms':>7} {'mean':>6} {'min':>6} "
          f"{'batch of 20':>12}")
    for n in names:
        t = times[n]
        row: dict[str, Any] = {"model": MODELS[n], "calls": len(t), "error": errors.get(n)}
        if t:
            row |= {"p50_ms": pct(t, 0.5), "p95_ms": pct(t, 0.95),
                    "mean_ms": round(statistics.mean(t), 1), "min_ms": round(min(t), 1),
                    "max_ms": round(max(t), 1),
                    "batch_20_ms": round(batch_ms[n], 1) if batch_ms[n] else None,
                    "cost_usd": round(emb[n].cost_usd, 8)}
            print(f"{MODELS[n]:<34} {len(t):>5} {row['p50_ms']:>7} {row['p95_ms']:>7} "
                  f"{row['mean_ms']:>6.0f} {row['min_ms']:>6.0f} "
                  f"{row['batch_20_ms'] or '-':>12}")
        else:
            print(f"{MODELS[n]:<34} {'-':>5}  refused: {errors.get(n, '?')[:60]}")
        out["results"][n] = row
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
