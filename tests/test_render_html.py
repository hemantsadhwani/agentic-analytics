"""The HTML report: every number on the page is a query value, the page is self-contained,
data is escaped, and the spec's new parts (tiles, decisions, charts) validate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_reporting.data import facts_from
from agentic_reporting.render_html import compact, full, nice_ticks, render_html
from agentic_reporting.run import main
from agentic_reporting.spec import ReportSpec, SectionSpec

ROOT = Path(__file__).resolve().parent.parent


def q(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows, "facts": facts_from(columns, rows), "dates_ok": []}


def state(drafts: dict[str, str], decisions_rows: list[list] | None = None) -> dict:
    spec = ReportSpec.model_validate({"title": "Review", "sections": [
        {"id": "headline", "heading": "Headline", "kind": "kpis", "sql": "SELECT 1",
         "prompt": "p", "tiles": [
             {"label": "Sales", "value": "sales", "format": "usd", "vs_prior": "sales_vs_prior_pct"},
             {"label": "Orders", "value": "orders", "vs_prior": "orders_vs_prior_pct"}]},
        {"id": "decisions", "heading": "Needs a decision", "kind": "decisions",
         "sql": "SELECT 1", "prompt": "p"},
        {"id": "trend", "heading": "Trend", "sql": "SELECT 1", "prompt": "p",
         "chart": {"type": "line", "x": "month", "y": ["a", "b"], "format": "usd"}},
        {"id": "regions", "heading": "Regions", "sql": "SELECT 1", "prompt": "p",
         "chart": {"type": "hbar", "x": "region", "y": ["sales"], "format": "usd"}},
    ]}).model_dump()
    return {"spec": spec, "spec_name": "review", "generated_at": "2026-11-16T00:00:00+00:00",
            "queries": {
                "headline": q(["month", "sales", "sales_vs_prior_pct", "orders", "orders_vs_prior_pct"],
                              [["2013-11", 5197154.91, 4.2, 2267, -1.9]]),
                "decisions": q(["severity", "area", "issue", "sales", "threshold"],
                               decisions_rows if decisions_rows is not None else
                               [["critical", "Reseller, Bikes", "Sold below cost", 2716128.86, 0]]),
                "trend": q(["month", "a", "b"], [["2013-10", 1.0e6, 3.3e6], ["2013-11", 1.78e6, 3.42e6]]),
                "regions": q(["region", "sales"], [["<script>alert(1)</script>", 993840.24],
                                                   ["Canada", 721444.34]]),
            },
            "drafts": {k: {"text": v} for k, v in drafts.items()}}


TEXTS = {"headline": "Sales were 5,197,154.91, up 4.2% on October.",
         "decisions": "Reseller bikes sold below cost.",
         "trend": "Internet sales reached 1,780,000.",
         "regions": "Canada sold 721,444.34 and Atlantis sold 999,999."}


def test_the_page_draws_numbers_from_rows_and_marks_unchecked_prose() -> None:
    page = render_html(state(TEXTS))
    assert page.count("<svg") == 2                                  # one per chart section
    assert "$5.2M" in page and "2,267" in page                     # tiles from the row
    assert 'class="bad"' in page and 'class="good"' in page        # orders down, sales up
    assert "November 2013" in page and "vs Oct 2013" in page
    assert "<mark" in page and "999,999" not in page               # unsupported number removed
    assert "1 unverified" in page


def test_the_page_is_self_contained_and_escapes_data() -> None:
    page = render_html(state(TEXTS))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert not re.search(r'(src|href)\s*=\s*"https?:', page)       # nothing from the network
    assert "@import" not in page and "url(" not in page


def test_no_flagged_rows_says_all_clear() -> None:
    page = render_html(state(TEXTS, decisions_rows=[]))
    assert "No rule flagged anything this month" in page


def test_formatting() -> None:
    assert compact(2267, "count") == "2,267"           # in full under 10,000
    assert compact(5197154.91, "usd") == "$5.2M"
    assert compact(993840.24, "usd") == "$993.8K"
    assert compact(0, "usd") == "$0"
    assert compact(13.9, "pct") == "13.9%"
    assert full(5197154.91, "usd") == "$5,197,154.91"
    assert full(None, "usd") == "–"
    assert nice_ticks(3.42e6) == [0, 1e6, 2e6, 3e6, 4e6]


def test_spec_parts_validate() -> None:
    with pytest.raises(ValidationError, match="needs tiles"):
        SectionSpec(id="a", heading="A", sql="SELECT 1", prompt="p", kind="kpis")
    with pytest.raises(ValidationError, match="tiles need kind kpis"):
        SectionSpec(id="a", heading="A", sql="SELECT 1", prompt="p",
                    tiles=[{"label": "x", "value": "v"}])
    with pytest.raises(ValidationError):
        SectionSpec(id="a", heading="A", sql="SELECT 1", prompt="p",
                    chart={"type": "pie", "x": "a", "y": ["b"]})
    with pytest.raises(ValidationError):
        SectionSpec(id="a", heading="A", sql="SELECT 1", prompt="p",
                    chart={"type": "line", "x": "a", "y": ["b", "c", "d", "e"]})


def test_a_missing_chart_column_is_said_not_crashed() -> None:
    s = state(TEXTS)
    s["spec"]["sections"][2]["chart"]["y"] = ["a", "nope"]
    assert "Chart not drawn: the query has no column nope" in render_html(s)


def test_publish_writes_html_beside_markdown(tmp_path: Path, sales_db: Path) -> None:
    out = tmp_path / "out"
    assert main(["--spec", str(ROOT / "specs" / "sales_mbr.yaml"), "--db", str(sales_db),
                 "--out", str(out), "--checkpoint-db", str(tmp_path / "cp.sqlite"),
                 "--provider", "fake", "--no-gate"]) == 0
    pages = list(out.rglob("*.html"))
    assert len(pages) == 1 and pages[0].with_suffix(".md").is_file()
    assert "Published without review" in pages[0].read_text(encoding="utf-8")


def test_the_adventureworks_spec_is_valid() -> None:
    from agentic_reporting.spec import load_spec
    spec = load_spec(ROOT / "specs" / "aw_monthly.yaml")
    kinds = [s.kind for s in spec.sections]
    assert kinds[:2] == ["kpis", "decisions"] and sum(1 for s in spec.sections if s.chart) == 4
