"""Report generation as a LangGraph ``StateGraph``: checkpointed, repair-once, human-gated.

    START → load → query_all → draft ⟲ (one section per step) → check ─┬→ repair → check
                                                                       │  (unsupported numbers
                                                                       │   and repairs_left > 0)
                                                                       └→ gate ─┬→ render → publish → END
                                                                                │   (approve / --no-gate)
                                                                                └→ reject → END

* ``load`` validates the YAML spec (single-SELECT SQL only), hashes its bytes (the spec
  version) and fixes ``generated_at``.
* ``query_all`` runs every section's SQL against a read-only SQLite URI.
* ``draft`` makes one structured provider call for the next undrafted section and loops.
  One section per super-step means each finished section is checkpointed: a crash on
  section 3 resumes at section 3, not at section 1.
* ``check`` is deterministic: every number in the text must match one of that section's facts.
* ``repair`` re-drafts only the failing sections, once, with the unsupported list in the prompt.
* ``gate`` calls ``interrupt()`` with a preview and whatever is still unsupported; nothing is
  written until a human resumes with ``approve``. ``no_gate=True`` skips the pause and
  auto-publishes; anything still unsupported is published as ``[unverified]``.
* ``render`` replaces any number still unsupported with ``[unverified]`` and records it;
  ``publish`` writes ``<out_dir>/<spec_name>/<YYYY-MM-DD>.md`` and the same report as a
  self-contained ``.html`` page (tiles, charts, tables), and one ``audit`` row.
* ``reject`` writes one ``audit`` row with ``decision="rejected"`` and no file.

State is plain JSON, so ``SqliteSaver`` round-trips it. The provider object is not
checkpointed: nodes rebuild it from ``state["provider"]`` unless one is injected (tests).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt

from agentic_reporting import audit
from agentic_reporting.check import check_section, row_labels
from agentic_reporting.data import run_query
from agentic_reporting.draft import draft_section
from agentic_reporting.render import render
from agentic_reporting.spec import load_spec, spec_version

DEFAULT_OUT = "reports"
DEFAULT_APPROVER = "local-user"
REPAIRS = 1


class ReportState(TypedDict, total=False):
    thread: str
    spec_path: str
    spec_name: str
    spec_version: str
    spec: dict[str, Any]
    db_path: str
    out_dir: str
    audit_db: str | None
    provider: str
    model_label: str | None
    no_gate: bool
    generated_at: str
    queries: dict[str, dict[str, Any]]
    cursor: int
    drafts: dict[str, dict[str, Any]]
    draft_failures: dict[str, str]
    checks: dict[str, dict[str, Any]]
    repairs_left: int
    repaired: list[str]
    budget: dict[str, Any]
    stop_reason: str | None
    approval: str | None
    decided_by: str | None
    report_md: str
    unverified: list[dict[str, str]]
    numbers_checked: int
    published_path: str | None


def initial_state(spec_path: str, db_path: str | None = None, out_dir: str = DEFAULT_OUT,
                  provider: str = "fake", no_gate: bool = False,
                  max_usd: float | None = None, max_llm_calls: int | None = None,
                  repairs: int = REPAIRS, thread: str = "", audit_db: str | None = None
                  ) -> ReportState:
    return ReportState(
        thread=thread, spec_path=str(spec_path), spec_name=Path(spec_path).stem,
        db_path=db_path or "", out_dir=out_dir, audit_db=audit_db, provider=provider,
        model_label=None, no_gate=no_gate, repairs_left=repairs, repaired=[], queries={},
        cursor=0, drafts={}, draft_failures={}, checks={},
        budget={"max_usd": max_usd, "max_llm_calls": max_llm_calls,
                "spent_usd": 0.0, "llm_calls": 0},
        stop_reason=None, approval=None, decided_by=None, unverified=[], numbers_checked=0,
        published_path=None,
    )


def make_provider(name: str | None) -> Any:
    from agentic_reporting.providers import get_provider
    return get_provider(name or "fake")


# ------------------------------------------------------------------ nodes
def load_node(state: ReportState) -> dict[str, Any]:
    spec = load_spec(state["spec_path"])
    db = state.get("db_path") or spec.db
    if not db:
        raise ValueError("no data source: pass --db or set `db:` in the spec")
    return {"spec": spec.model_dump(), "db_path": db,
            "spec_version": spec_version(state["spec_path"]),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}


def query_all_node(state: ReportState) -> dict[str, Any]:
    return {"queries": {s["id"]: run_query(state["db_path"], s["sql"]).to_dict()
                        for s in state["spec"]["sections"]}}


def _draft_many(state: ReportState, provider: Any, ids: list[str],
                unsupported: dict[str, list[str]] | None = None) -> dict[str, Any]:
    provider = provider or make_provider(state.get("provider"))
    budget = dict(state["budget"])
    drafts = dict(state.get("drafts", {}))
    failures = dict(state.get("draft_failures", {}))
    stop = state.get("stop_reason")
    label = state.get("model_label")
    by_id = {s["id"]: s for s in state["spec"]["sections"]}
    for sid in ids:
        prev = drafts.get(sid, {}).get("text")
        d, why = draft_section(provider, by_id[sid], state["queries"][sid], budget,
                               (unsupported or {}).get(sid), prev)
        if d is not None:
            drafts[sid] = d
            failures.pop(sid, None)
            label = label or d.get("provider")
        else:
            failures[sid] = why or "failed"
            if why and why.startswith("budget"):
                stop = "budget"
    return {"drafts": drafts, "draft_failures": failures, "budget": budget,
            "stop_reason": stop, "model_label": label}


def draft_node(state: ReportState, provider: Any = None) -> dict[str, Any]:
    """Draft the section at ``cursor`` and advance it (the edge loops back until done)."""
    i = state.get("cursor", 0)
    sid = state["spec"]["sections"][i]["id"]
    out = _draft_many(state, provider, [sid])
    out["cursor"] = i + 1
    return out


def check_node(state: ReportState) -> dict[str, Any]:
    checks = {}
    for s in state["spec"]["sections"]:
        sid, q = s["id"], state["queries"][s["id"]]
        d = state.get("drafts", {}).get(sid)
        if d is None:
            continue
        checks[sid] = check_section(d["text"], q["facts"], q["dates_ok"],
                                    d.get("numbers_used") or [],
                                    row_labels(q.get("columns", []), q.get("rows", []))).to_dict()
    return {"checks": checks}


def _problems(c: dict[str, Any]) -> list[str]:
    return ([*c["unsupported"]] + [f"{t} (not cited)" for t in c.get("uncited", [])]
            + [f"citation {b} does not match its fact" for b in c.get("bad_citations", [])])


def repair_node(state: ReportState, provider: Any = None) -> dict[str, Any]:
    failing = {sid: _problems(c) for sid, c in state["checks"].items() if not c["ok"]}
    out = _draft_many(state, provider, list(failing), failing)
    out["repairs_left"] = state.get("repairs_left", 0) - 1
    out["repaired"] = [*state.get("repaired", []), *failing]
    return out


def unsupported_by_section(state: ReportState) -> dict[str, list[str]]:
    return {sid: c["unsupported"] for sid, c in state.get("checks", {}).items()
            if c["unsupported"]}


def failing_by_section(state: ReportState) -> dict[str, list[str]]:
    """Every reason a section fails: unsupported, uncited, or a citation that lies."""
    return {sid: _problems(c) for sid, c in state.get("checks", {}).items() if not c["ok"]}


def warnings_by_section(state: ReportState) -> dict[str, list[str]]:
    """Attribution hints for the reviewer: true numbers whose column or row is not named."""
    return {sid: c["warnings"] for sid, c in state.get("checks", {}).items()
            if c.get("warnings")}


def gate_node(state: ReportState) -> dict[str, Any]:
    """Pause for a human. Resume value: ``"approve"`` / ``"reject"`` or
    ``{"decision": ..., "by": ...}`` (the CLI sends the dict with ``$REPORT_APPROVER``)."""
    if state.get("no_gate"):
        return {"approval": "auto (--no-gate)", "decided_by": "none (--no-gate)"}
    # Re-run from the top on resume: everything above interrupt() must be pure.
    preview, _, _ = render(state)
    answer = interrupt({
        "report_preview": preview,
        "unsupported": unsupported_by_section(state),
        "citation_problems": {sid: p for sid, p in failing_by_section(state).items()
                              if p != unsupported_by_section(state).get(sid)},
        "attribution_warnings": warnings_by_section(state),
        "draft_failures": state.get("draft_failures", {}),
        "question": "publish?",
    })
    if isinstance(answer, dict):
        decision, by = answer.get("decision"), answer.get("by")
    else:
        decision, by = answer, None
    ok = isinstance(decision, str) and decision.strip().lower() == "approve"
    return {"approval": "approved" if ok else "rejected",
            "decided_by": by or os.environ.get("REPORT_APPROVER") or DEFAULT_APPROVER}


def render_node(state: ReportState) -> dict[str, Any]:
    md, unverified, checked = render(state)
    return {"report_md": md, "unverified": unverified, "numbers_checked": checked}


def report_path(state: ReportState) -> Path:
    day = (state.get("generated_at") or datetime.now(UTC).isoformat())[:10]
    return Path(state.get("out_dir") or DEFAULT_OUT) / state["spec_name"] / f"{day}.md"


def audit_row(state: ReportState, decision: str, published: str | None) -> dict[str, Any]:
    label = state.get("model_label") or f"{state.get('provider') or '?'}/?"
    prov, _, model = label.partition("/")
    budget = state.get("budget", {})
    if published is None:     # rejected: count what the reviewer saw
        _, unverified, checked = render(state)
    else:
        unverified, checked = state.get("unverified", []), state.get("numbers_checked", 0)
    return {
        "thread": state.get("thread") or "?", "spec": state["spec_name"],
        "spec_version": state.get("spec_version") or "?", "provider": prov,
        "model": model or "?", "usd": float(budget.get("spent_usd") or 0.0),
        "llm_calls": int(budget.get("llm_calls") or 0),
        "sections": len(state["spec"]["sections"]), "numbers_checked": checked,
        "unverified": len(unverified), "decision": decision,
        "decided_by": state.get("decided_by") or "?", "published_path": published,
    }


def publish_node(state: ReportState) -> dict[str, Any]:
    path = report_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state["report_md"], encoding="utf-8")   # idempotent on a replay
    from agentic_reporting.render_html import render_html
    path.with_suffix(".html").write_text(render_html(state), encoding="utf-8")
    if state.get("audit_db"):
        audit.record(state["audit_db"], audit_row(state, state.get("approval") or "?", str(path)))
    return {"published_path": str(path),
            "stop_reason": state.get("stop_reason") or "published"}


def reject_node(state: ReportState) -> dict[str, Any]:
    if state.get("audit_db"):
        audit.record(state["audit_db"], audit_row(state, "rejected", None))
    return {"stop_reason": state.get("stop_reason") or "rejected"}


def route_after_draft(state: ReportState) -> str:
    return "draft" if state.get("cursor", 0) < len(state["spec"]["sections"]) else "check"


def route_after_check(state: ReportState) -> str:
    if failing_by_section(state) and state.get("repairs_left", 0) > 0:
        return "repair"
    return "gate"


def route_after_gate(state: ReportState) -> str:
    return "reject" if state.get("approval") == "rejected" else "render"


# ------------------------------------------------------------------ build
# Transient provider errors (connection resets, SDK API errors) are retried; LangGraph's
# default retry_on skips ValueError / RuntimeError / OSError-style failures, and
# draft_section turns schema failures into a recorded section failure instead of raising.
DRAFT_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.5)


def build_graph(checkpointer=None, provider: Any = None):
    """Compile the report graph. ``checkpointer=None`` uses an in-memory saver (the gate's
    ``interrupt()`` needs one). ``provider`` injects a provider into draft/repair (tests)."""
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()

    def draft(state: ReportState) -> dict[str, Any]:
        return draft_node(state, provider)

    def repair(state: ReportState) -> dict[str, Any]:
        return repair_node(state, provider)

    g = StateGraph(ReportState)
    g.add_node("load", load_node)
    g.add_node("query_all", query_all_node)
    g.add_node("draft", draft, retry_policy=DRAFT_RETRY)
    g.add_node("check", check_node)
    g.add_node("repair", repair, retry_policy=DRAFT_RETRY)
    g.add_node("gate", gate_node)
    g.add_node("render", render_node)
    g.add_node("publish", publish_node)
    g.add_node("reject", reject_node)

    g.add_edge(START, "load")
    g.add_edge("load", "query_all")
    g.add_edge("query_all", "draft")
    g.add_conditional_edges("draft", route_after_draft, ["draft", "check"])
    g.add_conditional_edges("check", route_after_check, ["repair", "gate"])
    g.add_edge("repair", "check")
    g.add_conditional_edges("gate", route_after_gate, ["render", "reject"])
    g.add_edge("render", "publish")
    g.add_edge("publish", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer)


def config(thread: str) -> dict[str, Any]:
    """Run config for a thread. The draft loop is one step per section, so set an explicit
    recursion limit well above any reasonable section count. Explicit, because the library
    default changed across the pinned range (25 in early langgraph 1.0, 10,000+ from 1.1)."""
    return {"configurable": {"thread_id": thread}, "recursion_limit": 200}
