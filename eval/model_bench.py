"""Pick the cheapest model that passes: both golden reports through each Bedrock model.

Cheapest per token is not cheapest per report. A model that writes reasoning tokens, needs
the repair call, or breaks the JSON schema costs more per *passing* report. So, per model:

* first-draft pass: sections the checker accepted with no repair;
* final pass: sections clean after the one repair (no unsupported, uncited or bad citation);
* draft failures: sections the model could not produce in the schema at all;
* attribution warnings: true numbers whose column or row is not named (a meaning hint);
* tokens in / out, USD (token counts from Bedrock x the price row), wall-clock seconds.

Score = USD per passing report (a report passes when every section is clean after repair).

Real calls, real money (cents). Needs AWS credentials (``AWS_PROFILE``) and Bedrock in
``ap-south-1``. Usage::

    AWS_PROFILE=agentic python -m eval.model_bench                 # all candidates
    AWS_PROFILE=agentic python -m eval.model_bench --models deepseek.v3.2 google.gemma-3-12b-it
    ... --out eval/results/model_bench_2026-11-15.json

Not a CI gate: CI stays offline. The JSON result is committed as evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.report_eval import GOLDEN_DIR, build_sample, load_golden

CANDIDATES = [
    "google.gemma-3-4b-it", "google.gemma-3-12b-it", "google.gemma-3-27b-it",
    "mistral.ministral-3-8b-instruct", "mistral.ministral-3-14b-instruct",
    "nvidia.nemotron-nano-3-30b", "openai.gpt-oss-20b-1:0", "openai.gpt-oss-120b-1:0",
    "qwen.qwen3-32b-v1:0", "zai.glm-4.7-flash", "deepseek.v3.2",
]


class Metered:
    """Wraps a provider; records every call's tokens, cost, mode and latency."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.calls = inner, []
        self.name, self.model = inner.name, inner.model

    def complete(self, **kw: Any) -> Any:
        t = time.perf_counter()
        r = self.inner.complete(**kw)
        self.calls.append({"in": r.input_tokens, "out": r.output_tokens, "usd": r.cost_usd,
                           "mode": r.structured_mode, "s": time.perf_counter() - t})
        return r


def run_report(golden: dict[str, Any], db: Path, out: Path, provider: Any) -> dict[str, Any]:
    from agentic_reporting.graph import build_graph, config, initial_state

    graph = build_graph(provider=provider)
    thread = f"bench-{golden['spec']}-{provider.model}"
    graph.invoke(initial_state(str(Path(__file__).resolve().parents[1] / golden["spec"]),
                               db_path=str(db), out_dir=str(out), provider=provider.name,
                               no_gate=True, thread=thread), config(thread))
    return graph.get_state(config(thread)).values


def bench_model(model: str, goldens: list[dict[str, Any]], dbs: dict[str, Path],
                out: Path) -> dict[str, Any]:
    from agentic_reporting.providers.converse_provider import BedrockConverseProvider

    prov = Metered(BedrockConverseProvider(model=model))
    sections = first_ok = final_ok = failures = warnings = reports_ok = 0
    errors: list[str] = []
    problems: list[dict[str, Any]] = []
    warn_samples: list[str] = []
    required = have = 0
    t0 = time.perf_counter()
    for g in goldens:
        try:
            st = run_report(g, dbs[g["sample"]], out, prov)
        except Exception as e:                      # an outage or a refused model: record it
            errors.append(f"{g['spec']}: {type(e).__name__}: {str(e)[:160]}")
            continue
        n = len(st["spec"]["sections"])
        repaired = set(st.get("repaired", []))
        fails = st.get("draft_failures", {})
        clean = [sid for sid, c in st.get("checks", {}).items() if c["ok"]]
        sections += n
        failures += len(fails)
        first_ok += n - len(repaired | set(fails))
        final_ok += len(clean)
        warnings += sum(len(c.get("warnings", [])) for c in st.get("checks", {}).values())
        # Coverage: a clean section that skips the facts it was asked for is not a pass.
        missing = []
        for gs in g["sections"]:
            used = set((st.get("checks", {}).get(gs["id"]) or {}).get("matched", {}).values())
            need = gs["required_fact_prefixes"]
            covered = [p for p in need if any(k.startswith(p) for k in used)]
            required += len(need)
            have += len(covered)
            missing += [f"{gs['id']}:{p}" for p in need if p not in covered]
        reports_ok += int(len(clean) == n and not fails and not missing)
        if missing:
            problems.append({"spec": g["spec"], "missing_facts": missing})
        for sid, c in st.get("checks", {}).items():
            for w in c.get("warnings", [])[:2]:
                warn_samples.append(f"{g['spec']}:{sid}: {w}")
        for sid, c in st.get("checks", {}).items():
            if not c["ok"]:
                problems.append({"spec": g["spec"], "section": sid,
                                 "unsupported": c["unsupported"], "uncited": c.get("uncited"),
                                 "bad_citations": c.get("bad_citations"),
                                 "text": st["drafts"][sid]["text"]})
        for sid, why in fails.items():
            problems.append({"spec": g["spec"], "section": sid, "draft_failure": why})
    usd = sum(c["usd"] for c in prov.calls)
    return {
        "model": model, "reports": len(goldens), "reports_passed": reports_ok,
        "sections": sections, "first_draft_pass": first_ok, "final_pass": final_ok,
        "draft_failures": failures, "attribution_warnings": warnings,
        "required_facts": required, "required_facts_covered": have,
        "warning_samples": warn_samples[:12],
        "calls": len(prov.calls), "tokens_in": sum(c["in"] for c in prov.calls),
        "tokens_out": sum(c["out"] for c in prov.calls),
        "modes": sorted({c["mode"] for c in prov.calls}),
        "usd": round(usd, 6),
        "usd_per_passing_report": round(usd / reports_ok, 6) if reports_ok else None,
        "seconds": round(time.perf_counter() - t0, 1), "errors": errors,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    ap.add_argument("--out", default=None, help="write the JSON result here")
    ap.add_argument("--repeats", type=int, default=1,
                    help="run each golden report N times per model (models are not deterministic)")
    a = ap.parse_args(argv)

    goldens = [load_golden(p) for p in sorted(GOLDEN_DIR.glob("*.json"))
               if not p.name.startswith("schema_")]
    work = Path(tempfile.mkdtemp(prefix="model_bench_"))
    dbs = {g["sample"]: build_sample(g["sample"], work) for g in goldens}
    rows = []
    for m in a.models:
        r = bench_model(m, goldens * a.repeats, dbs, work / "out")
        rows.append(r)
        per = r["usd_per_passing_report"]
        print(f"{m:36s} pass {r['reports_passed']}/{r['reports']}  first {r['first_draft_pass']}/"
              f"{r['sections']}  final {r['final_pass']}/{r['sections']}  fail {r['draft_failures']}"
              f"  cover {r['required_facts_covered']}/{r['required_facts']}"
              f"  warn {r['attribution_warnings']}  tok {r['tokens_in']}/{r['tokens_out']}  "
              f"${r['usd']:.5f}  per-pass {'-' if per is None else f'${per:.5f}'}  "
              f"{r['seconds']}s {'ERR ' + r['errors'][0] if r['errors'] else ''}", flush=True)
    ranked = sorted((r for r in rows if r["usd_per_passing_report"] is not None),
                    key=lambda r: r["usd_per_passing_report"])
    if ranked:
        print(f"\ncheapest passing: {ranked[0]['model']} at "
              f"${ranked[0]['usd_per_passing_report']:.5f} per report")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({
            "run_at": datetime.now(UTC).isoformat(timespec="seconds"), "region": "ap-south-1",
            "pricing": "AWS Price List API, ap-south-1, read 2026-11-15",
            "repeats": a.repeats,
            "results": rows}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
