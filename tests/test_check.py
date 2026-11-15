"""The deterministic number checker: positives, negatives, tolerance, %, years, ids."""

from __future__ import annotations

from agentic_reporting.check import check_section, redact

FACTS = {"revenue[0]": 33661.95, "units[0]": 155, "change_pct[0]": -9.8,
         "share[0]": 0.125, "zero[0]": 0}


def unsupported(text, facts=FACTS, dates_ok=None):
    return check_section(text, facts, dates_ok).unsupported


def test_exact_and_formatted_numbers_pass():
    r = check_section("North made 33,661.95 on 155 units, down -9.8.", FACTS)
    assert r.ok and r.numbers_checked == 3
    assert r.matched["155"] == "units[0]"


def test_invented_number_fails():
    assert unsupported("North made 40,000 in revenue.") == ["40,000"]


def test_tolerance_is_half_a_percent():
    assert unsupported("revenue 33,662") == []          # 0.001% off
    assert unsupported("revenue 33,700") == []          # 0.11% off
    assert unsupported("revenue 34,000") == ["34,000"]  # 1.0% off
    assert unsupported("zero 0") == []


def test_percent_matches_either_scale():
    assert unsupported("change 9.8% on the month") == ["9.8%"]  # sign matters: fact is -9.8
    assert unsupported("down 9.8% on the month") == []          # direction word sets the sign
    assert unsupported("up 9.8% on the month") == ["9.8%"]      # ... and catches the wrong one
    assert unsupported("down -9.8% on the month") == []         # explicit sign wins
    assert unsupported("a 12.5% share") == []                   # 12.5% == 0.125
    assert unsupported("a 13% share") == []                     # 12.5 rounded to whole %
    assert unsupported("a 14% share") == ["14%"]


def test_years_only_when_the_query_returned_them():
    assert unsupported("In 2026 revenue was 33,661.95.", dates_ok=[2026]) == []
    assert unsupported("In 2031 revenue was 33,661.95.", dates_ok=[2026]) == ["2031"]
    assert unsupported("Month 2026-09 closed.", dates_ok=[2026]) == []
    assert unsupported("Month 2024-09 closed.", dates_ok=[2026]) == ["2024-09"]


def test_identifiers_list_markers_and_citations_are_not_numbers():
    text = "1. SEV1 on payments-api v2 and g3-v2; see revenue[0].\n2) nothing else."
    assert check_section(text, {}).numbers_checked == 0


def test_bad_citation_fails_the_section():
    r = check_section("units 155", FACTS, numbers_used=[{"value": 156, "fact_key": "units[0]"},
                                                         {"value": 1, "fact_key": "nope[0]"}])
    assert not r.ok and r.bad_citations == ["units[0]=156", "nope[0]=1"]
    assert r.uncited == ["155"]            # the only citation for 155 was wrong


def test_citations_are_enforced_when_given():
    good = [{"value": 155, "fact_key": "units[0]"}]
    assert check_section("units 155", FACTS, numbers_used=good).ok
    r = check_section("units 155", FACTS, numbers_used=[])
    assert not r.ok and r.uncited == ["155"] and r.unsupported == []
    assert "155 (not cited)" in r.problems()
    assert check_section("units 155", FACTS).ok      # None = enforcement off (unit use)


def test_redact_replaces_only_unsupported():
    text, removed = redact("units 155 and 987,654.3 next quarter", FACTS)
    assert text == "units 155 and [unverified] next quarter"
    assert removed == ["987,654.3"]
