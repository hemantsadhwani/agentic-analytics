"""CLI: draft a report from a spec, pause at the human gate, publish on approval.

    python -m agentic_reporting.run --spec specs/sales_mbr.yaml --db samples/sales.sqlite
    python -m agentic_reporting.run --resume --thread <t> --decision approve
    python -m agentic_reporting.run --resume --thread <t>          # continue a crashed run
    python -m agentic_reporting.run --list                         # audit log + paused threads
    python -m agentic_reporting.run --spec ... --db ... --no-gate  # CI: auto-publish

A fresh run executes until the human gate and stops with the exact resume command. The
pause lives in the checkpoint SQLite (``--checkpoint-db``), so the approval can come from
another shell, another pod, or tomorrow. ``--db`` is the *data source* (opened read-only);
``--checkpoint-db`` is where the run state and the audit table live.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from langgraph.types import Command

from agentic_reporting import audit
from agentic_reporting.budget import footer as budget_footer
from agentic_reporting.checkpoint import DEFAULT_CHECKPOINT_DB, default_checkpointer
from agentic_reporting.graph import (
    DEFAULT_APPROVER,
    DEFAULT_OUT,
    build_graph,
    config,
    failing_by_section,
    initial_state,
    unsupported_by_section,
    warnings_by_section,
)
from agentic_reporting.providers.router import PROVIDERS, resolve_name
from agentic_reporting.render import render
from agentic_reporting.render_html import render_html

PREVIEW_CHARS = 110


def _db_path(arg: str | None) -> str:
    return arg or os.environ.get("REPORT_CHECKPOINT_DB") or str(DEFAULT_CHECKPOINT_DB)


def _approver() -> str:
    return os.environ.get("REPORT_APPROVER") or DEFAULT_APPROVER


def _pending(snap) -> tuple[str, ...]:
    """Nodes a checkpointed thread still has to run (including tasks whose writes were
    saved but whose step checkpoint was not, i.e. a process killed between the two)."""
    return tuple(snap.next) or tuple(t.name for t in snap.tasks)


def _paused(snap) -> bool:
    return tuple(snap.next) == ("gate",) and any(t.interrupts for t in snap.tasks)


def _stream(graph, payload, cfg) -> None:
    """Run to the next stop. ``durability="sync"`` persists each step's checkpoint before
    the next step starts, so a crash loses at most the section in flight."""
    for chunk in graph.stream(payload, cfg, stream_mode="updates", durability="sync"):
        for node, update in chunk.items():
            if not isinstance(update, dict):
                continue
            if node == "draft":
                state = graph.get_state(cfg).values
                total = len(state["spec"]["sections"])
                i = update.get("cursor", 0)
                sid = state["spec"]["sections"][i - 1]["id"]
                why = (update.get("draft_failures") or {}).get(sid)
                print(f"  · draft {i}/{total} {sid}" + (f" FAILED ({why})" if why else ""))
            elif node == "check":
                bad = {s: c["unsupported"] for s, c in update["checks"].items()
                       if c["unsupported"]}
                n = sum(c["numbers_checked"] for c in update["checks"].values())
                print(f"  · check: {n} numbers, "
                      + (f"unsupported {bad}" if bad else "all traced to a query"))
            elif node == "repair":
                print(f"  · repair: re-drafted {', '.join(update.get('repaired', [])) or '-'}")


def _summary(state: dict[str, Any]) -> tuple[int, int, int]:
    if state.get("report_md"):
        unverified, checked = state.get("unverified", []), state.get("numbers_checked", 0)
    else:
        _, unverified, checked = render(state)
    return len(state.get("spec", {}).get("sections", [])), checked, len(unverified)


def _print_report_line(state: dict[str, Any]) -> None:
    n, m, k = _summary(state)
    print(f"report: {n} sections, {m} numbers checked, {k} unverified")


def _preview(state: dict[str, Any]) -> None:
    drafts, failures = state.get("drafts", {}), state.get("draft_failures", {})
    for sec in state["spec"]["sections"]:
        d = drafts.get(sec["id"])
        text = d["text"] if d else f"(not drafted: {failures.get(sec['id'], '?')})"
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS - 1] + "…"
        print(f"  | {sec['heading']}: {text}")


def _outcome(graph, cfg, thread: str, db: str) -> int:
    snap = graph.get_state(cfg)
    state = snap.values
    if _paused(snap):
        print(f"PAUSED thread={thread} (publish?)")
        _preview(state)
        try:    # the approver reads the same page; kept beside the checkpoint, never in --out
            page = Path(db).parent / "previews" / f"{thread}.html"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(render_html(state), encoding="utf-8")
            print(f"  preview: {page}")
        except OSError as e:
            print(f"  preview not written: {e}")
        unsupported = unsupported_by_section(state)
        print(f"  unsupported: {unsupported or 'none'}"
              + ("  (will publish as [unverified] if approved)" if unsupported else ""))
        cites = {s: [p for p in ps if p not in unsupported.get(s, [])]
                 for s, ps in failing_by_section(state).items()}
        cites = {s: ps for s, ps in cites.items() if ps}
        if cites:
            print(f"  citation problems after repair: {cites}")
        for sid, ws in warnings_by_section(state).items():
            for w in ws:
                print(f"  check meaning [{sid}]: {w}")
        if state.get("draft_failures"):
            print(f"  not drafted: {state['draft_failures']}")
        db_flag = "" if db == str(DEFAULT_CHECKPOINT_DB) else f" --checkpoint-db {db}"
        print(f"  approve: python -m agentic_reporting.run --resume --thread {thread} "
              f"--decision approve{db_flag}   (or --decision reject)")
    elif state.get("published_path"):
        print(f"PUBLISHED {state['published_path']}")
    elif state.get("approval") == "rejected":
        print(f"REJECTED thread={thread}")
    else:
        print(f"STOPPED thread={thread} (next: {', '.join(_pending(snap)) or '-'})")
    _print_report_line(state)
    stop = state.get("stop_reason") or ("paused" if _paused(snap) else None)
    print(budget_footer(state.get("budget", {}), stop))
    return 0


def _crashed(thread: str, db: str, err: Exception) -> int:
    db_flag = "" if db == str(DEFAULT_CHECKPOINT_DB) else f" --checkpoint-db {db}"
    print(f"CRASHED thread={thread}: {type(err).__name__}: {str(err)[:200]}", file=sys.stderr)
    print(f"  continue from the last checkpoint: python -m agentic_reporting.run --resume "
          f"--thread {thread}{db_flag}", file=sys.stderr)
    return 1


def _list(graph, saver, db: str) -> int:
    rows = audit.recent(db)
    print(f"audit ({db}):")
    if not rows:
        print("  (no decisions yet)")
    for r in rows:
        print(f"  {r['created_at']}  {r['thread']:<34} {r['decision']:<16} by {r['decided_by']:<12} "
              f"spec {r['spec']}@{r['spec_version'][:8]}  {r['provider']}/{r['model']}  "
              f"${r['usd']:.4f} {r['llm_calls']} calls  {r['sections']} sections  "
              f"{r['numbers_checked']} checked  {r['unverified']} unverified")
    decided = {r["thread"] for r in audit.recent(db, limit=10_000)}
    open_threads: list[str] = []
    for tup in saver.list(None):
        t = tup.config["configurable"]["thread_id"]
        if t not in open_threads and t not in decided:
            open_threads.append(t)
    waiting = []
    for t in open_threads:
        snap = graph.get_state(config(t))
        if _paused(snap):
            waiting.append(f"  {t}  PAUSED at gate")
        elif _pending(snap):
            waiting.append(f"  {t}  unfinished (next: {', '.join(_pending(snap))}) "
                           "-> --resume to continue")
    if waiting:
        print("open threads:")
        print("\n".join(waiting))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m agentic_reporting.run",
        description="Draft a report from read-only SQL facts, check every number, "
                    "pause for a human, publish, audit.")
    ap.add_argument("--spec", help="report spec YAML (e.g. specs/sales_mbr.yaml)")
    ap.add_argument("--db", help="data source: SQLite file, opened read-only "
                                 "(default: the spec's `db:`)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="output directory; reports go to <out>/<spec>/<YYYY-MM-DD>.md")
    ap.add_argument("--checkpoint-db", metavar="PATH",
                    help="run state + audit table (default: $REPORT_CHECKPOINT_DB or "
                         f"{DEFAULT_CHECKPOINT_DB})")
    ap.add_argument("--thread", help="thread id (default: <spec>-<UTC timestamp>); "
                                     "required with --resume")
    ap.add_argument("--resume", action="store_true",
                    help="with --decision: answer a thread paused at the gate; without: "
                         "continue a crashed or interrupted thread from its last checkpoint")
    ap.add_argument("--decision", choices=["approve", "reject"],
                    help="with --resume; the approver name comes from $REPORT_APPROVER "
                         f"(default {DEFAULT_APPROVER})")
    ap.add_argument("--list", action="store_true",
                    help="print recent audit rows and threads still open")
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the human gate and AUTO-PUBLISH: numbers the checker cannot "
                         "trace to a query are replaced with [unverified]. For CI and evals; "
                         "the audit row says decision='auto (--no-gate)'")
    ap.add_argument("--provider", choices=list(PROVIDERS),
                    help="LLM provider (default: $LLM_PROVIDER, else anthropic if "
                         "ANTHROPIC_API_KEY is set, else the offline fake)")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="stop drafting once estimated spend reaches this (default: unbounded)")
    ap.add_argument("--max-llm-calls", type=int, default=None,
                    help="stop drafting after this many LLM calls (default: unbounded)")
    return ap


def main(argv: list[str] | None = None, provider: Any = None) -> int:
    """CLI entry point. ``provider`` injects a provider object (tests only)."""
    ap = build_parser()
    args = ap.parse_args(argv)
    db = _db_path(args.checkpoint_db)
    saver = default_checkpointer(db)
    graph = build_graph(saver, provider=provider)

    if args.list:
        return _list(graph, saver, db)

    if args.resume:
        if not args.thread:
            ap.error("--resume needs --thread <id> (see --list)")
        return _resume(graph, args, db)
    if args.decision:
        ap.error("--decision only makes sense with --resume --thread <id>")
    if not args.spec:
        ap.error("--spec is required for a new run")

    name = getattr(provider, "name", "injected") if provider is not None else resolve_name(args.provider)
    stem = Path(args.spec).stem
    thread = args.thread or f"{stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    cfg = config(thread)
    if graph.get_state(cfg).values:
        print(f"thread {thread!r} already exists in {db}; pass a new --thread or --resume it",
              file=sys.stderr)
        return 2
    print(f"run thread={thread} spec={args.spec} provider={name}"
          + (" (no gate: auto-publish)" if args.no_gate else ""))
    try:
        _stream(graph, initial_state(
            args.spec, db_path=args.db, out_dir=args.out, provider=name,
            no_gate=args.no_gate, max_usd=args.max_usd, max_llm_calls=args.max_llm_calls,
            thread=thread, audit_db=db), cfg)
    except Exception as e:   # noqa: BLE001 - report the crash and how to resume, exit 1
        return _crashed(thread, db, e)
    return _outcome(graph, cfg, thread, db)


def _resume(graph, args, db: str) -> int:
    thread = args.thread
    cfg = config(thread)
    snap = graph.get_state(cfg)
    if not snap.values:
        print(f"no thread {thread!r} in {db}", file=sys.stderr)
        return 2
    if args.decision:
        if not _paused(snap):
            hint = " (it is mid-run: drop --decision to continue it)" if _pending(snap) else ""
            print(f"thread {thread!r} is not paused at the gate{hint}", file=sys.stderr)
            return 2
        payload: Any = Command(resume={"decision": args.decision, "by": _approver()})
    else:
        if _paused(snap):
            print(f"thread {thread!r} is paused at the gate: pass --decision approve|reject",
                  file=sys.stderr)
            return 2
        pending = _pending(snap)
        if not pending:
            print(f"thread {thread!r} already finished "
                  f"({snap.values.get('approval') or snap.values.get('stop_reason') or 'done'})",
                  file=sys.stderr)
            return 2
        print(f"  · continuing thread {thread} from checkpoint (next: {', '.join(pending)})")
        payload = None
    try:
        _stream(graph, payload, cfg)
    except Exception as e:   # noqa: BLE001
        return _crashed(thread, db, e)
    return _outcome(graph, cfg, thread, db)


if __name__ == "__main__":
    sys.exit(main())
