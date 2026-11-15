"""Mutation test for the checker: corrupt every number in an honest section, many ways.

Each mutant keeps the model's original citations, as a real model's stale citation list
would. A mutant is *equivalent* when its value is still within 0.5% of the true value
(computed here independently of the checker, e.g. swapping two digits deep in a large
number); the checker is allowed to pass those, by its tolerance rule. Every other mutant
must fail the check. The test prints the score so the README can quote it.
"""

from __future__ import annotations

import re

from agentic_reporting.check import check_section, scan

FACTS = {"revenue[0]": 111850.2, "orders[0]": 160, "change_pct[0]": -9.8,
         "aov[0]": 699.06, "north_revenue[0]": 41213.5, "returns[0]": 7}
CITED = [{"value": v, "fact_key": k} for k, v in FACTS.items()]
TEXT = ("In 2026-09 revenue fell 9.8% to 111,850.2 on 160 orders. Average order value (aov) "
        "was 699.06. North revenue led with 41,213.5. Returns were 7.")
DATES = [2026]


def fmt_like(orig: str, v: float) -> str:
    """Write ``v`` in the style of ``orig``: same decimals, commas, percent sign."""
    body = orig.rstrip("%")
    dec = len(body.split(".")[1]) if "." in body else 0
    s = f"{v:,.{dec}f}" if "," in body else f"{v:.{dec}f}"
    return s + ("%" if orig.endswith("%") else "")


def mutants(text: str):
    """Yield (name, mutated_text, true_value, mutated_value) for every number in ``text``."""
    for h in scan(text, FACTS, DATES):
        tok, a, b = h.token, h.start, h.end
        signed = FACTS[h.fact_key]
        mag = abs(float(signed))
        sign = -1 if signed < 0 else 1
        for k in (1.02, 0.97, 10, 0.1, 1.5):
            written = fmt_like(tok, mag * k)           # "7" * 1.02 is still written "7"
            yield f"scale{k}:{tok}", text[:a] + written + text[b:], signed, \
                sign * float(written.rstrip("%").replace(",", ""))
        digits = [i for i, c in enumerate(tok) if c.isdigit()]
        for i, j in zip(digits, digits[1:], strict=False):
            if j == i + 1 and tok[i] != tok[j]:
                t2 = tok[:i] + tok[j] + tok[i] + tok[j + 1:]
                v2 = sign * float(t2.rstrip("%").replace(",", ""))
                yield f"swap:{tok}->{t2}", text[:a] + t2 + text[b:], signed, v2
        before = text[max(0, a - 6):a]
        if "fell" in before:
            yield f"direction:{tok}", text[:a].replace("fell", "rose") + text[a:], signed, -signed
        else:
            yield f"negate:{tok}", text[:a] + "-" + text[a:], signed, -float(signed)
        kk = round(mag * 1.5 / 1000) or 1
        yield f"suffix-k:{tok}", text[:a] + f"{kk}k" + text[b:], signed, sign * kk * 1000
    yield "word:160", text.replace("160", "one hundred and seventy"), 160, 170
    yield "word:41,213.5", text.replace("41,213.5", "forty-five thousand"), 41213.5, 45000


def equivalent(true: float, mutated: float) -> bool:
    return abs(mutated - true) <= max(1e-9, 0.005 * abs(true))


def test_honest_text_passes_clean():
    r = check_section(TEXT, FACTS, DATES, CITED)
    assert r.ok and r.numbers_checked == 6 and r.warnings == []


def test_every_non_equivalent_mutant_is_killed(capsys):
    total, killed, equiv, survivors = 0, 0, 0, []
    for name, text, true, mutated in mutants(TEXT):
        total += 1
        if equivalent(true, mutated):
            equiv += 1
            continue
        if check_section(text, FACTS, DATES, CITED).ok:
            survivors.append(name)
        else:
            killed += 1
    with capsys.disabled():
        print(f"\nchecker mutation score: {killed}/{total - equiv} killed "
              f"({equiv} equivalent within 0.5%, {total} mutants)")
    assert survivors == []
    assert total - equiv >= 40


def test_mutating_the_citation_alone_is_caught():
    bad = [dict(c) for c in CITED]
    bad[1]["value"] = 161                                  # orders cited as 161
    r = check_section(TEXT, FACTS, DATES, bad)
    assert not r.ok and r.bad_citations == ["orders[0]=161"]


def test_dropping_a_citation_is_caught():
    r = check_section(TEXT, FACTS, DATES, [c for c in CITED if c["fact_key"] != "aov[0]"])
    assert not r.ok and r.uncited == ["699.06"]


def test_redaction_never_lets_a_mutant_through():
    from agentic_reporting.check import redact
    for _, text, true, mutated in mutants(TEXT):
        if equivalent(true, mutated):
            continue
        out, removed = redact(text, FACTS, DATES)
        assert check_section(out, FACTS, DATES).unsupported == [], (text, out)
        if removed:
            assert re.search(r"\[unverified\]", out)


def test_coarse_rounding_cannot_borrow_another_fact():
    # "1k orders" would round to aov 699.06 under a pure +-500 precision rule; capped at 5%.
    r = check_section("Orders were 1k.", FACTS, DATES)
    assert r.unsupported == ["1k"]
