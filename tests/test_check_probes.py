"""Every probe from the 2026-11-12 checker audit, pinned as a regression test.

The audit ran hand-written sentences against the v1 checker and found escapes (``112k``,
``3x``, number words, impossible dates) and false positives (``fell 9.8%``, ``1.1 lakh``,
Indian grouping, ``-10%``). Each row below is one probe and its v2 verdict.
"""

from __future__ import annotations

import pytest

from agentic_reporting.check import check_section, row_labels

# Facts shaped like the sales summary: this month (row 0) and the prior month (row 1).
COLUMNS = ["month", "revenue", "orders", "change_pct"]
ROWS = [["2026-09", 111850.2, 160, -9.8], ["2026-08", 124013.2, 171, None]]
FACTS = {"revenue[0]": 111850.2, "orders[0]": 160, "change_pct[0]": -9.8,
         "revenue[1]": 124013.2, "orders[1]": 171}
DATES = [2026]


def check(text, cited=None):
    return check_section(text, FACTS, DATES, cited, row_labels(COLUMNS, ROWS))


PROBES = [
    # (text, ok, unsupported, numbers_checked)
    ("Revenue fell 9.8% to 111,850.2.", True, [], 2),             # v1: false positive
    ("Revenue declined by 9.8% to 111,850.2.", True, [], 2),
    ("Revenue saw a 9.8% decline.", True, [], 1),
    ("Revenue rose 9.8% to 111,850.2.", False, ["9.8%"], 2),      # wrong direction
    ("Revenue was 112,000.", True, [], 1),                         # 0.13% off: rounding
    ("Revenue was 111,300.", True, [], 1),                         # 0.49%
    ("Revenue was 111,250.", False, ["111,250"], 1),               # 0.54%
    ("Revenue was about 112k.", True, [], 1),                      # v1: escaped, 0 checked
    ("Revenue was about 150k.", False, ["150k"], 1),
    ("Revenue was 0.11 million.", True, [], 1),                    # v1: false positive
    ("Revenue was 1.1 lakh.", True, [], 1),                        # v1: false positive
    ("Revenue was 1.5 lakh.", False, ["1.5 lakh"], 1),
    ("Revenue was 1,11,850.2.", True, [], 1),                      # Indian grouping
    ("Q3 showed 3x growth over Q2.", False, ["3x"], 1),            # v1: escaped
    ("Orders were one hundred and sixty.", True, [], 1),           # v1: escaped
    ("Orders were one hundred and seventy.", False, ["one hundred and seventy"], 1),
    ("Orders were twelve thousand.", False, ["twelve thousand"], 1),
    ("Revenue grew three times.", False, ["three times"], 1),
    ("Three regions reported.", False, ["Three"], 1),              # every count is checked
    ("One of the regions reported.", True, [], 0),                 # a bare "one" is a pronoun
    ("Orders rose by four on the month.", False, ["four"], 1),     # derived, in words (real run)
    ("Change was -10%.", True, [], 1),                             # rounding of -9.8
    ("Change was -11%.", False, ["-11%"], 1),
    ("Revenue for 2026-13 was 111,850.2.", False, ["2026-13"], 2),  # impossible month
    ("Revenue fell by 12,163.", False, ["12,163"], 1),             # derived: by design
    ("Latency was 250ms.", False, ["250ms"], 1),                   # units are checked
    ("Build 73fc of payments-api v2 shipped on the 3rd.", True, [], 0),  # identifiers
    # Found by the first real-model run (DeepSeek V3.2 on Bedrock, 2026-11-15):
    ("Revenue was 111,850.2, a decrease of 9.8% from the prior month.", True, [], 2),
    ("Revenue was 111,850.2, an increase of 9.8% from the prior month.", False, ["9.8%"], 2),
    ("For the week ending September 29, 2026 revenue was 111,850.2.", True, [], 1),
    ("For the week ending 29 Sep 2026 revenue was 111,850.2.", True, [], 1),
    ("For the week ending September 29, 2031 revenue was 111,850.2.", False,
     ["September 29, 2031"], 2),
]


@pytest.mark.parametrize("text,ok,unsupported,checked", PROBES)
def test_probe(text, ok, unsupported, checked):
    r = check(text)
    assert (r.ok, r.unsupported, r.numbers_checked) == (ok, unsupported, checked)


def test_row_count_is_a_fact_of_multi_row_results():
    # "the top five products" when five rows came back (Gemma and Qwen wrote this, 2026-11-15)
    labels = {i: [f"p{i}"] for i in range(5)}
    r = check_section("The top five products follow.", {"revenue[0]": 1.0}, [], [], labels)
    assert r.ok and r.matched == {"five": "row_count"} and r.uncited == []
    r = check_section("The top six products follow.", {"revenue[0]": 1.0}, [], [], labels)
    assert r.unsupported == ["six"]


def test_true_number_wrong_metric_is_flagged_for_the_reviewer():
    r = check("Revenue was 160.", [{"value": 160, "fact_key": "orders[0]"}])
    assert r.ok                                       # every digit traces to a fact ...
    assert any("'orders' is not named" in w for w in r.warnings)   # ... but not this claim


def test_true_number_wrong_row_is_flagged_for_the_reviewer():
    text = "In 2026-09 orders were 160 and revenue was 124,013.2."
    r = check(text, [{"value": 160, "fact_key": "orders[0]"},
                     {"value": 124013.2, "fact_key": "revenue[1]"}])
    assert r.ok
    assert [w for w in r.warnings if "revenue[1]" in w and "2026-08" in w]
    assert not [w for w in r.warnings if "orders[0]" in w]


def test_honest_sentence_has_no_warnings():
    text = "In 2026-09 revenue fell 9.8% to 111,850.2 on 160 orders."
    r = check(text, [{"value": 111850.2, "fact_key": "revenue[0]"},
                     {"value": 160, "fact_key": "orders[0]"},
                     {"value": -9.8, "fact_key": "change_pct[0]"}])
    assert r.ok and r.warnings == [] and r.numbers_checked == 3
