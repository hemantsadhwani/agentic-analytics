"""Render the checked report as Markdown. Unsupported numbers never reach the page.

Each section's final text goes through ``check.redact``: any number that does not match a
fact of that section's query is replaced with ``[unverified]`` and listed in the returned
``unverified`` records. The Sources block lists, per section, every fact the text used
(fact key -> the exact value the query returned), derived by the checker, not by the model.
"""

from __future__ import annotations

from typing import Any

from agentic_reporting.check import redact, scan, with_row_count
from agentic_reporting.data import is_number

MARK = "[unverified]"


def _value(v: Any) -> str:
    return repr(v) if isinstance(v, float) else str(v)


def render(state: dict[str, Any]) -> tuple[str, list[dict[str, str]], int]:
    """Return ``(markdown, unverified records, numbers_checked)`` for the current state."""
    spec = state["spec"]
    queries, drafts = state.get("queries", {}), state.get("drafts", {})
    failures = state.get("draft_failures", {})
    body: list[str] = []
    sources: list[str] = []
    unverified: list[dict[str, str]] = []
    checked = 0
    for sec in spec["sections"]:
        sid = sec["id"]
        q = queries.get(sid, {"facts": {}, "dates_ok": []})
        facts = with_row_count(q.get("facts", {}), len(q.get("rows", [])))
        dates_ok = q.get("dates_ok", [])
        body += [f"## {sec['heading']}", ""]
        d = drafts.get(sid)
        if not d:
            body += [f"_Not drafted: {failures.get(sid, 'no draft')}._", ""]
            continue
        hits = scan(d["text"], facts, dates_ok)
        checked += len(hits)
        text, removed = redact(d["text"], facts, dates_ok, MARK)
        unverified += [{"section": sid, "token": t} for t in removed]
        body += [text, ""]
        used = sorted({h.fact_key for h in hits if h.fact_key and "[" in h.fact_key},
                      key=lambda k: (k.split("[")[0], int(k.split("[")[1].rstrip("]"))))
        if used:
            sources += [f"### {sec['heading']}", "", "| fact | value |", "|---|---|"]
            sources += [f"| `{k}` | {_value(facts[k])} |" for k in used if is_number(facts[k])]
            sources.append("")

    head = [
        f"# {spec['title']}",
        "",
        f"_Generated {state.get('generated_at', '?')} · spec `{state.get('spec_name', '?')}` · "
        f"data `{state.get('db_path', '?')}` (read-only) · {checked} numbers checked · "
        f"{len(unverified)} unverified_",
        "",
    ]
    if unverified:
        per: dict[str, int] = {}
        for u in unverified:
            per[u["section"]] = per.get(u["section"], 0) + 1
        # The unverified values themselves are deliberately NOT printed on the page (the
        # reviewer saw them at the gate, and the audit/checkpoint keeps them).
        head += [f"> **{len(unverified)} number(s) could not be traced to a query result and "
                 f"were replaced with `{MARK}`** in: "
                 + ", ".join(f"{sid} ({n})" for sid, n in per.items()), ""]
    tail = ["---", "", "## Sources", "",
            "Every number above matched (within 0.5%) a cell returned by that section's "
            "read-only SQL query.", ""] + (sources or ["_No numbers used._", ""])
    tail += ["---", "", footer_line(state)]
    return "\n".join(head + body + tail).rstrip() + "\n", unverified, checked


def footer_line(state: dict[str, Any]) -> str:
    """Provenance: spec version (sha256 of the YAML bytes), provider/model, cost, decision."""
    budget = state.get("budget") or {}
    version = (state.get("spec_version") or "?")[:12]
    parts = [f"spec `{state.get('spec_name', '?')}` version `sha256:{version}`",
             f"provider `{state.get('model_label') or state.get('provider') or '?'}`",
             f"cost ${float(budget.get('spent_usd') or 0.0):.4f} "
             f"({int(budget.get('llm_calls') or 0)} llm calls, list-price estimate)"]
    if state.get("approval"):
        parts.append(f"decision `{state['approval']}` by `{state.get('decided_by') or '?'}`")
    if state.get("thread"):
        parts.append(f"thread `{state['thread']}`")
    return "_" + " · ".join(parts) + "_"
