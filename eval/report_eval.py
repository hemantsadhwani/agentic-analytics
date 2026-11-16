"""Golden-report eval: the CI gate for the whole pipeline.

    python -m eval.report_eval --fake

For every golden file in ``eval/datasets/golden/`` it builds the SYNTHETIC sample database in
a temp dir and runs the real graph (load → query → draft → check → repair → render →
publish, with ``--no-gate``) three times:

1. ``FactsFakeProvider``: the published report must contain every golden section, with
   zero ``[unverified]`` marks; every number in each section's text must appear in that
   section's Sources table; each section's Sources must cite the golden fact-key prefixes.
2. ``FabricatingProvider(repairable=True)``: the first draft of every section carries a
   made-up number; the checker must catch it, the repair must clear it, and the published
   report must again have zero unverified numbers and no trace of the fabricated value.
3. ``FabricatingProvider()``: the model keeps fabricating through the repair; the made-up
   number must never reach the page. Every section must publish it as ``[unverified]``.

``--fake`` proves the pipeline and the checker, not a model's prose. Exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "datasets" / "golden"
SOURCE_ROW_RE = re.compile(r"^\| `([^`]+)` \| (.+) \|$")


def load_golden(path: Path) -> dict[str, Any]:
    g = json.loads(path.read_text(encoding="utf-8"))
    try:
        assert isinstance(g["spec"], str) and g["sample"] in ("sales", "ops")
        for s in g["sections"]:
            assert isinstance(s["id"], str) and isinstance(s["heading"], str)
            assert all(isinstance(p, str) for p in s["required_fact_prefixes"])
    except (AssertionError, KeyError, TypeError) as e:
        raise ValueError(f"{path.name}: malformed golden file ({e!r})") from e
    return g


def build_sample(kind: str, where: Path) -> Path:
    from samples import make_ops_db, make_sales_db
    builder = make_sales_db if kind == "sales" else make_ops_db
    return builder.build(where / f"{kind}.sqlite")


def parse_report(md: str) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    """``({heading: section text}, {heading: {fact_key: value}})`` from a published report."""
    body, _, rest = md.partition("\n---\n")
    sections: dict[str, str] = {}
    for block in re.split(r"(?m)^## ", body)[1:]:
        heading, _, text = block.partition("\n")
        sections[heading.strip()] = text.strip()
    sources: dict[str, dict[str, float]] = {}
    current = None
    for line in rest.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sources[current] = {}
        elif current and (m := SOURCE_ROW_RE.match(line.strip())):
            sources[current][m.group(1)] = float(m.group(2))
    return sections, sources


def run_once(golden: dict[str, Any], db: Path, out: Path, provider: Any) -> dict[str, Any]:
    from agentic_reporting.graph import build_graph, config, initial_state

    graph = build_graph(provider=provider)
    thread = f"eval-{Path(golden['spec']).stem}-{type(provider).__name__}"
    graph.invoke(initial_state(str(ROOT / golden["spec"]), db_path=str(db), out_dir=str(out),
                               provider=provider.name, no_gate=True, thread=thread,
                               audit_db=str(out / "audit.sqlite")), config(thread))
    return graph.get_state(config(thread)).values


def check_clean(golden: dict[str, Any], state: dict[str, Any], label: str) -> list[str]:
    """Failures for a run that must publish with zero unverified numbers."""
    from agentic_reporting.check import scan
    from agentic_reporting.providers.fake import FABRICATED

    fails: list[str] = []
    path = state.get("published_path")
    if not path or not Path(path).is_file():
        return [f"{label}: no published report"]
    md = Path(path).read_text(encoding="utf-8")
    sections, sources = parse_report(md)
    ids = [s["id"] for s in state["spec"]["sections"]]
    want = [s["id"] for s in golden["sections"]]
    if ids != want:
        fails.append(f"{label}: spec sections {ids} != golden {want}")
    if state.get("unverified") or "[unverified]" in md:
        fails.append(f"{label}: {len(state.get('unverified', []))} unverified number(s)")
    if FABRICATED in md:
        fails.append(f"{label}: fabricated value {FABRICATED} reached the page")
    for g in golden["sections"]:
        text = sections.get(g["heading"])
        if text is None:
            fails.append(f"{label}: section {g['id']!r} ({g['heading']}) missing")
            continue
        facts = sources.get(g["heading"], {})
        dates_ok = state["queries"][g["id"]]["dates_ok"]
        stray = [h.token for h in scan(text, facts, dates_ok) if h.fact_key is None]
        if stray:
            fails.append(f"{label}: {g['id']}: numbers not in Sources: {stray}")
        for prefix in g["required_fact_prefixes"]:
            if not any(k.startswith(prefix) for k in facts):
                fails.append(f"{label}: {g['id']}: Sources cite no {prefix}…] fact")
    return fails


def check_fabricating(golden: dict[str, Any], state: dict[str, Any], label: str) -> list[str]:
    """Failures for a run whose model fabricates through the repair."""
    from agentic_reporting.providers.fake import FABRICATED

    fails: list[str] = []
    path = state.get("published_path")
    if not path or not Path(path).is_file():
        return [f"{label}: no published report"]
    md = Path(path).read_text(encoding="utf-8")
    n = len(golden["sections"])
    if FABRICATED in md:
        fails.append(f"{label}: fabricated value {FABRICATED} reached the page")
    marked = {u["section"] for u in state.get("unverified", [])}
    if len(marked) != n or md.count("[unverified]") < n:
        fails.append(f"{label}: expected every one of {n} sections marked [unverified], "
                     f"got {sorted(marked)}")
    if sorted(state.get("repaired", [])) != sorted(g["id"] for g in golden["sections"]):
        fails.append(f"{label}: repair did not run for every section")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fake", action="store_true",
                    help="offline providers only (the only mode today; required)")
    args = ap.parse_args(argv)
    if not args.fake:
        ap.error("pass --fake (a real-provider golden run is not implemented yet)")

    from agentic_reporting.providers.fake import FabricatingProvider, FactsFakeProvider

    # schema_questions_*.json belong to eval/schema_linking_eval.py, not to this gate.
    goldens = sorted(p for p in GOLDEN_DIR.glob("*.json")
                     if not p.name.startswith(("schema_questions_", "chat_questions_")))
    if not goldens:
        print(f"REPORT-EVAL FAIL: no golden files in {GOLDEN_DIR}")
        return 1
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="report-eval-") as tmp:
        tmpdir = Path(tmp)
        for gpath in goldens:
            golden = load_golden(gpath)
            name = Path(golden["spec"]).stem
            db = build_sample(golden["sample"], tmpdir / "data")
            runs = [
                ("facts", FactsFakeProvider(), check_clean),
                ("fabricate-once", FabricatingProvider(repairable=True), check_clean),
                ("fabricate-always", FabricatingProvider(), check_fabricating),
            ]
            for tag, provider, checker in runs:
                label = f"{name}/{tag}"
                state = run_once(golden, db, tmpdir / tag, provider)
                fails = checker(golden, state, label)
                failures += fails
                print(f"  {'ok  ' if not fails else 'FAIL'} {label:<30} "
                      f"{len(state['spec']['sections'])} sections · "
                      f"{state.get('numbers_checked', 0)} numbers checked · "
                      f"{len(state.get('unverified', []))} unverified · "
                      f"repaired {len(state.get('repaired', []))} · "
                      f"{provider.name}/{provider.model} {len(provider.calls)} calls")
    for f in failures:
        print(f"  - {f}")
    if failures:
        print(f"REPORT-EVAL FAIL ({len(failures)} failure(s))")
        return 1
    print(f"REPORT-EVAL PASS ({len(goldens)} golden reports × 3 providers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
