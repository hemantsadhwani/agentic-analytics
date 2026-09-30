"""The report graph end to end: pause at the gate, approve/reject, audit, repair, budget."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from langgraph.types import Command  # noqa: E402

from agentic_reporting import audit  # noqa: E402
from agentic_reporting.graph import build_graph, config, initial_state  # noqa: E402
from agentic_reporting.providers.fake import (  # noqa: E402
    FABRICATED,
    FabricatingProvider,
    FactsFakeProvider,
)
from conftest import OPS_SPEC, SALES_SPEC  # noqa: E402


def _start(tmp_path, db, provider, spec=SALES_SPEC, thread="t", **kw):
    graph = build_graph(provider=provider)
    audit_db = tmp_path / "cp.sqlite"
    graph.invoke(initial_state(str(spec), db_path=str(db), out_dir=str(tmp_path / "out"),
                               provider=provider.name, thread=thread, audit_db=str(audit_db),
                               **kw), config(thread))
    return graph, audit_db


def test_pauses_at_gate_and_writes_nothing(tmp_path, sales_db):
    graph, audit_db = _start(tmp_path, sales_db, FactsFakeProvider())
    snap = graph.get_state(config("t"))
    assert snap.next == ("gate",)
    payload = snap.tasks[0].interrupts[0].value
    assert payload["question"] == "publish?" and payload["unsupported"] == {}
    assert "## Revenue by region" in payload["report_preview"]
    assert not (tmp_path / "out").exists()
    assert audit.recent(audit_db) == []


def test_approve_publishes_file_and_one_audit_row(tmp_path, sales_db):
    graph, audit_db = _start(tmp_path, sales_db, FactsFakeProvider())
    graph.invoke(Command(resume={"decision": "approve", "by": "alice"}), config("t"))
    state = graph.get_state(config("t")).values
    path = tmp_path / "out" / "sales_mbr" / f"{state['generated_at'][:10]}.md"
    assert state["published_path"] == str(path) and path.is_file()
    md = path.read_text(encoding="utf-8")
    assert "0 unverified" in md and "decision `approved` by `alice`" in md
    assert f"sha256:{state['spec_version'][:12]}" in md
    rows = audit.recent(audit_db)
    assert len(rows) == 1
    r = rows[0]
    assert (r["thread"], r["decision"], r["decided_by"], r["provider"], r["model"]) == \
        ("t", "approved", "alice", "fake", "facts-fake-1")
    assert r["sections"] == 4 and r["unverified"] == 0 and r["numbers_checked"] > 20
    assert r["spec_version"] == state["spec_version"] and r["llm_calls"] == 4


def test_reject_writes_no_file_and_one_audit_row(tmp_path, ops_db):
    graph, audit_db = _start(tmp_path, ops_db, FactsFakeProvider(), spec=OPS_SPEC)
    graph.invoke(Command(resume={"decision": "reject", "by": "bob"}), config("t"))
    state = graph.get_state(config("t")).values
    assert state["approval"] == "rejected" and state.get("published_path") is None
    assert not (tmp_path / "out").exists()
    rows = audit.recent(audit_db)
    assert [(r["decision"], r["decided_by"], r["published_path"]) for r in rows] == \
        [("rejected", "bob", None)]


def test_audit_insert_is_idempotent_per_thread(tmp_path):
    row = {"thread": "x", "spec": "s", "spec_version": "v", "provider": "fake", "model": "m",
           "usd": 0.0, "llm_calls": 1, "sections": 1, "numbers_checked": 1, "unverified": 0,
           "decision": "approved", "decided_by": "a", "published_path": None}
    assert audit.record(tmp_path / "a.sqlite", row) is True
    assert audit.record(tmp_path / "a.sqlite", row) is False     # a replay adds nothing
    assert len(audit.recent(tmp_path / "a.sqlite")) == 1


def test_repair_clears_a_fabricated_number(tmp_path, sales_db):
    provider = FabricatingProvider(repairable=True)
    graph, _ = _start(tmp_path, sales_db, provider)
    payload = graph.get_state(config("t")).tasks[0].interrupts[0].value
    assert payload["unsupported"] == {}                  # the repair already fixed it
    state = graph.get_state(config("t")).values
    assert sorted(state["repaired"]) == sorted(s["id"] for s in state["spec"]["sections"])
    assert len(provider.calls) == 8                      # 4 drafts + 4 repairs
    assert "REPAIR:" in provider.calls[-1]["messages"][-1]["content"]
    assert FABRICATED in provider.calls[-1]["messages"][-1]["content"]   # quoted back


def test_persistent_fabrication_is_published_as_unverified(tmp_path, sales_db):
    graph, audit_db = _start(tmp_path, sales_db, FabricatingProvider(), no_gate=True)
    state = graph.get_state(config("t")).values
    md = (tmp_path / "out" / "sales_mbr" / f"{state['generated_at'][:10]}.md").read_text()
    assert FABRICATED not in md and md.count("[unverified]") >= 4
    assert len(state["unverified"]) == 4 and state["approval"] == "auto (--no-gate)"
    assert audit.recent(audit_db)[0]["unverified"] == 4


def test_gate_shows_unsupported_when_repair_is_exhausted(tmp_path, sales_db):
    graph, _ = _start(tmp_path, sales_db, FabricatingProvider())
    payload = graph.get_state(config("t")).tasks[0].interrupts[0].value
    assert set(payload["unsupported"]) == {"summary", "revenue_by_region", "top_products",
                                           "month_on_month"}
    assert all(v == ["987,654.3"] for v in payload["unsupported"].values())


def test_budget_stops_drafting_and_says_so(tmp_path, sales_db):
    provider = FactsFakeProvider()
    graph, _ = _start(tmp_path, sales_db, provider, max_llm_calls=2)
    state = graph.get_state(config("t")).values
    assert len(provider.calls) == 2 and state["stop_reason"] == "budget"
    assert set(state["draft_failures"]) == {"top_products", "month_on_month"}
    assert all(v.startswith("budget exhausted") for v in state["draft_failures"].values())
