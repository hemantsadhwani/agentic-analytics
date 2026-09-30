"""The deterministic number checker. No LLM, no network: regex + arithmetic.

Every number written in a section's text must match a fact (a numeric cell the section's
own query returned) within 0.5% relative tolerance, or 1e-9 absolute for zeros. Anything
else is *unsupported*. The checker never trusts the model's own ``numbers_used`` claims;
those are verified separately and reported as ``bad_citations``.

What counts as a number: ``-3.2``, ``1,234.5``, ``12%`` (matched as 12 or 0.12).
Digits glued to letters or hyphens are identifiers, not numbers (``v007``,
``nifty-expiry-s3``, ``g3-v2``). What is ignored:

* dates (``2026-09``, ``2026-09-30T10:05:11+00:00``) and bare 4-digit years, but only for
  years that appear in a date-like cell of the query result (``dates_ok``);
* a list marker at the start of a line (``1.`` / ``2)``): the section's own numbering;
* a fact-key citation written inline (``revenue[0]``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

REL_TOL = 0.005
ABS_TOL = 1e-9

NUMBER_RE = re.compile(
    r"(?<![\w.\-])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w%])")
DATE_TOKEN_RE = re.compile(
    r"(?<![\w\-])(\d{4})-\d{2}(?:-\d{2})?"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:\d{2})?)?(?![\w\-])")
LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)](?=\s)")
CITATION_RE = re.compile(r"\w+\[\d+\]")


@dataclass
class Hit:
    start: int
    end: int
    token: str
    fact_key: str | None      # None = unsupported


@dataclass
class CheckResult:
    ok: bool
    unsupported: list[str] = field(default_factory=list)
    matched: dict[str, str] = field(default_factory=dict)      # token -> fact_key
    bad_citations: list[str] = field(default_factory=list)
    numbers_checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_number(token: str) -> list[float]:
    """Candidate values for a token: ``12.5%`` -> [12.5, 0.125]."""
    pct = token.endswith("%")
    v = float(token.rstrip("%").replace(",", ""))
    return [v, v / 100.0] if pct else [v]


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * abs(b))


def find_fact(values: list[float], facts: dict[str, float]) -> str | None:
    for key, fv in facts.items():
        if any(close(v, float(fv)) for v in values):
            return key
    return None


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


def scan(text: str, facts: dict[str, float], dates_ok: list[int] | None = None) -> list[Hit]:
    """Every checked number in ``text`` with its span and the fact it matched (or None)."""
    ok_years = set(dates_ok or [])
    hits: list[Hit] = []
    masked: list[tuple[int, int]] = []
    for m in DATE_TOKEN_RE.finditer(text):
        masked.append(m.span())
        if int(m.group(1)) not in ok_years:
            hits.append(Hit(m.start(), m.end(), m.group(0), None))
    masked += [m.span() for m in LIST_MARKER_RE.finditer(text)]
    masked += [m.span() for m in CITATION_RE.finditer(text)]
    clean = _mask(text, masked)
    for m in NUMBER_RE.finditer(clean):
        tok = m.group(0)
        if re.fullmatch(r"\d{4}", tok) and int(tok) in ok_years:
            continue
        hits.append(Hit(m.start(), m.end(), tok, find_fact(parse_number(tok), facts)))
    return sorted(hits, key=lambda h: h.start)


def check_section(text: str, facts: dict[str, float], dates_ok: list[int] | None = None,
                  numbers_used: list[dict[str, Any]] | None = None) -> CheckResult:
    hits = scan(text, facts, dates_ok)
    unsupported = [h.token for h in hits if h.fact_key is None]
    matched = {h.token: h.fact_key for h in hits if h.fact_key is not None}
    bad = []
    for ref in numbers_used or []:
        key, val = ref.get("fact_key"), ref.get("value")
        if key not in facts or val is None or not close(float(val), float(facts[key])):
            bad.append(f"{key}={val}")
    return CheckResult(ok=not unsupported, unsupported=unsupported, matched=matched,
                       bad_citations=bad, numbers_checked=len(hits))


def redact(text: str, facts: dict[str, float], dates_ok: list[int] | None = None,
           mark: str = "[unverified]") -> tuple[str, list[str]]:
    """Replace every unsupported number with ``mark``; return (text, removed tokens)."""
    out, removed, pos = [], [], 0
    for h in scan(text, facts, dates_ok):
        if h.fact_key is None:
            out.append(text[pos:h.start])
            out.append(mark)
            removed.append(h.token)
            pos = h.end
    out.append(text[pos:])
    return "".join(out), removed
