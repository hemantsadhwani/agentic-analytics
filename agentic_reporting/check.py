"""The deterministic number checker. No LLM, no network: regex + arithmetic.

Every number written in a section's text must match a fact (a numeric cell the section's
own query returned). Anything else is *unsupported* and never reaches the page.

Tolerance. A plain number must be within 0.5% of the fact (1e-9 absolute for zeros). A
percentage or a scaled number (``112k``, ``1.1 lakh``, ``twelve thousand``) is also allowed
to be the fact *rounded to the precision written*: ``-10%`` for -9.8, ``112k`` for 111,850.2.

What counts as a number (v2, 2026-11):

* ``-3.2``, ``+5``, ``1,234.5``, Indian grouping ``1,11,850.2``;
* percentages ``12%`` / ``12 percent`` (matched as 12 or 0.12, at the written precision);
* scaled numbers: glued ``112k``, ``1.2M``, ``3bn``, ``2cr``, or spaced ``0.11 million``,
  ``1.1 lakh``, ``2 crore``; lower-case ``m`` may mean million or minutes, both are tried;
* multipliers and units: ``3x``, ``250ms``, ``4GB`` are checked by their number;
* number words: ``one hundred and sixty``, ``twelve thousand``, ``twenty-five``. Small
  counts under twenty in words (``three regions``) are allowed, because the drafting prompt
  asks for them, unless a multiplier or percent follows (``three times``, ``five percent``).

Direction words set the sign of an unsigned number: ``fell 9.8%`` / ``a 9.8% decline`` is
-9.8, ``rose 9.8%`` is +9.8, so ``up 9.8%`` against a fact of -9.8 fails. An explicit sign
wins. A level after ``to`` / ``from`` / ``at`` (``fell to 111,850``) keeps its own sign.

Ignored: dates (``2026-09``, ISO timestamps) and bare years that the query returned
(``dates_ok``); an impossible date (``2026-13``) is unsupported; list markers at the start of
a line; inline fact-key citations (``revenue[0]``); ordinals (``3rd``); identifiers (digits
glued to other letters: ``v007``, ``g3-v2``, ``73fc``).

Citations. ``check_section`` with ``numbers_used`` (the model's own claims) enforces them:
every number in the text must be cited by a fact key whose value it matches (else
*uncited*), and every citation must match its fact (else *bad citation*). Either fails the
section, so the graph sends it to repair. Citations decide which fact a number is
attributed to; the checker never trusts a citation it cannot verify.

Attribution hints (warnings, never failures): a number whose cited column is not named in
its sentence, or, for a multi-row result, whose row label (``North``, ``2026-08``) is not
named there. That is the "true number, wrong claim" case: a deterministic hint for the
human at the gate, not proof of meaning.

Known gaps, by design: words like ``twice``, ``half``, ``doubled``, ``a third``; derived
numbers (a difference the SQL did not return) are always unsupported.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

REL_TOL = 0.005
ABS_TOL = 1e-9

_NUM = r"(?:\d{1,2}(?:,\d{2})+,\d{3}|\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
# Glued suffixes that make a token a checked number. Anything else glued = identifier.
SCALE = {"k": [1e3], "m": [1e6, 1.0], "mn": [1e6], "b": [1e9], "bn": [1e9],
         "cr": [1e7], "x": [1.0]}
UNITS = {"ms", "s", "sec", "secs", "h", "hr", "hrs", "min", "mins", "d", "gb", "mb", "tb",
         "kb", "km", "kg", "pp", "bps", "rpm", "rps", "qps"}
ORDINALS = {"st", "nd", "rd", "th"}
SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "lakh": 1e5, "lakhs": 1e5,
               "lac": 1e5, "crore": 1e7, "crores": 1e7}

NUMBER_RE = re.compile(
    rf"(?<![\w.,\-+−])(?P<sign>[-+−])?(?P<num>{_NUM})(?![\d,]\d)"
    r"(?:(?P<pct>%)|\s+(?P<pctw>percent|per\s+cent)\b"
    rf"|\s+(?P<sw>{'|'.join(SCALE_WORDS)})\b|(?P<suf>[A-Za-z]+))?"
    r"(?![\w%])")
DATE_TOKEN_RE = re.compile(
    r"(?<![\w\-])(\d{4})-(\d{2})(?:-(\d{2}))?"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:\d{2})?)?(?![\w\-])")
LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)](?=\s)")
CITATION_RE = re.compile(r"\w+\[\d+\]")

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
WORD_VALUE = {w: i for i, w in enumerate(_ONES)} | {w: 20 + 10 * i for i, w in enumerate(_TENS)}
_WORD = "|".join(sorted([*WORD_VALUE, "hundred", *SCALE_WORDS], key=len, reverse=True))
WORDS_RE = re.compile(
    rf"(?i)\b(?:{_WORD})(?:(?:\s+and\s+|[\s\-]+)(?:{_WORD}))*\b"
    r"(?P<tail>\s+(?:times|fold|percent|per\s+cent)\b|%)?")

DOWN = r"fell|fall|falls|falling|down|declin\w*|decreas\w*|drop\w*|lower|lost|shr[ai]nk\w*|" \
       r"contract\w*|dipp?\w*|slipp?\w*|reduc\w*|cut"
UP = r"rose|rise|rises|rising|up|increas\w*|grew|grow\w*|gain\w*|higher|climb\w*|jump\w*|" \
     r"improv\w*"
DIR_BEFORE_RE = re.compile(rf"(?i)\b(?:(?P<down>{DOWN})|(?P<up>{UP}))\b(?:\s+\w+){{0,2}}\s*$")
DIR_AFTER_RE = re.compile(   # noun forms only: "a 9.8% decline", never "111,850 down 9.8%"
    r"(?i)^\s*(?:(?P<down>declines?|decreases?|drops?|falls?|dips?|reductions?|contractions?)"
    r"|(?P<up>increases?|rises?|gains?|growth|jumps?|improvements?))\b")
DIRECTION_RE = re.compile(rf"(?i)\b(?:{DOWN}|{UP})\b")
LEVEL_BEFORE_RE = re.compile(r"(?i)\b(?:to|from|at|of)\s*$")
SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


@dataclass
class Hit:
    start: int
    end: int
    token: str
    fact_key: str | None      # None = unsupported
    cands: list[tuple[float, float]] = field(default_factory=list, repr=False)  # (value, tol)


@dataclass
class CheckResult:
    ok: bool
    unsupported: list[str] = field(default_factory=list)
    matched: dict[str, str] = field(default_factory=dict)      # token -> fact_key
    bad_citations: list[str] = field(default_factory=list)
    uncited: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    numbers_checked: int = 0

    def problems(self) -> list[str]:
        """What a repair prompt is told: every reason this section failed."""
        return ([*self.unsupported] + [f"{t} (not cited)" for t in self.uncited]
                + [f"citation {c} does not match its fact" for c in self.bad_citations])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ parsing

def _decimals(num: str) -> int:
    return len(num.split(".")[1]) if "." in num else 0


def parse_number(token: str) -> list[float]:
    """Candidate values for a plain or percent token: ``12.5%`` -> [12.5, 0.125]."""
    pct = token.endswith("%")
    v = float(token.rstrip("%").replace(",", "").replace("−", "-"))
    return [v, v / 100.0] if pct else [v]


def _candidates(m: re.Match[str], sign: float) -> list[tuple[float, float]] | None:
    """(value, absolute tolerance) pairs for a numeric match; None = not a checked number."""
    num = m.group("num")
    v = sign * float(num.replace(",", ""))
    half = 0.5 * 10 ** -_decimals(num)         # half a unit of the last written digit
    if m.group("pct") or m.group("pctw"):
        return [(v, half), (v / 100.0, half / 100.0)]
    if m.group("sw"):
        s = SCALE_WORDS[m.group("sw").lower()]
        return [(v * s, half * s)]
    suf = m.group("suf")
    if suf is None:
        return [(v, 0.0)]
    low = suf.lower()
    if low in ORDINALS and "." not in num:
        return None
    if low in SCALE:
        scales = SCALE[low] if suf != "M" else [1e6]
        return [(v * s, half * s if s != 1.0 else 0.0) for s in scales]
    if low in UNITS:
        return [(v, 0.0)]
    return None                                  # identifier such as 73fc


def words_value(phrase: str) -> tuple[float, float]:
    """``one hundred and sixty`` -> (160, 0); ``twelve thousand`` -> (12000, 500)."""
    parts = [p for p in re.split(r"[\s\-]+", phrase.lower()) if p and p != "and"]
    total, cur, last_scale = 0.0, 0.0, 0.0
    for p in parts:
        if p in WORD_VALUE:
            cur += WORD_VALUE[p]
            last_scale = 0.0
        elif p == "hundred":
            cur = (cur or 1) * 100
            last_scale = 100.0
        else:
            total += (cur or 1) * SCALE_WORDS[p]
            cur = 0.0
            last_scale = SCALE_WORDS[p]
    return total + cur, 0.5 * last_scale if last_scale >= 1000 else 0.0


def _sign_from_context(text: str, start: int, end: int, floor: int) -> float | None:
    before = text[max(floor, start - 40):start]
    if LEVEL_BEFORE_RE.search(before):
        return None
    m = DIR_BEFORE_RE.search(before) or DIR_AFTER_RE.match(text[end:end + 20])
    if not m:
        return None
    return -1.0 if m.group("down") else 1.0


MAX_ROUNDING = 0.05     # a written precision never buys more than 5% of the fact: "1k" != 699


def close(a: float, b: float, tol: float = 0.0) -> bool:
    """Within 0.5% (or ABS_TOL); or within the written precision ``tol``, capped at 5%."""
    tol = min(tol, MAX_ROUNDING * abs(b)) * (1 + 1e-9)      # 1e-9: float noise at the edge
    return abs(a - b) <= max(ABS_TOL, REL_TOL * abs(b), tol)


def matches(cands: list[tuple[float, float]], fact: float) -> bool:
    return any(close(v, float(fact), tol) for v, tol in cands)


def find_fact(cands: list[tuple[float, float]], facts: dict[str, Any],
              prefer: list[str] | None = None) -> str | None:
    for key in [*(prefer or []), *facts]:
        fv = facts.get(key)
        if isinstance(fv, (int, float)) and not isinstance(fv, bool) and matches(cands, fv):
            return key
    return None


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


def _valid_date(m: re.Match[str]) -> bool:
    month, day = int(m.group(2)), m.group(3)
    return 1 <= month <= 12 and (day is None or 1 <= int(day) <= 31)


# ------------------------------------------------------------------ scanning

def scan(text: str, facts: dict[str, Any], dates_ok: list[int] | None = None,
         prefer: list[str] | None = None) -> list[Hit]:
    """Every checked number in ``text`` with its span and the fact it matched (or None)."""
    ok_years = set(dates_ok or [])
    hits: list[Hit] = []
    masked: list[tuple[int, int]] = []
    for m in DATE_TOKEN_RE.finditer(text):
        masked.append(m.span())
        if int(m.group(1)) not in ok_years or not _valid_date(m):
            hits.append(Hit(m.start(), m.end(), m.group(0), None))
    masked += [m.span() for m in LIST_MARKER_RE.finditer(text)]
    masked += [m.span() for m in CITATION_RE.finditer(text)]
    clean = _mask(text, masked)

    prev_end = 0
    numeric_spans: list[tuple[int, int]] = []
    for m in NUMBER_RE.finditer(clean):
        numeric_spans.append(m.span())
        tok = m.group(0)
        if re.fullmatch(r"\d{4}", tok) and int(tok) in ok_years:
            continue
        sign_txt = m.group("sign")
        if sign_txt:
            sign = -1.0 if sign_txt in "-−" else 1.0
        else:
            sign = _sign_from_context(clean, m.start(), m.end(), prev_end) or 1.0
        cands = _candidates(m, sign)
        if cands is None:
            continue
        hits.append(Hit(m.start(), m.end(), tok, find_fact(cands, facts, prefer), cands))
        prev_end = m.end()

    clean = _mask(clean, numeric_spans)          # "1.1 lakh" is one number, not two
    for m in WORDS_RE.finditer(clean):
        phrase = m.group(0)[: m.start("tail") - m.start()] if m.group("tail") else m.group(0)
        v, tol = words_value(phrase)
        tail = (m.group("tail") or "").strip().lower()
        if v < 20 and not tol and not tail:
            continue                       # "three regions": allowed by the prompt
        sign = _sign_from_context(clean, m.start(), m.end(), 0) or 1.0
        v *= sign
        cands = [(v, tol)] + ([(v / 100.0, 0.0)] if tail in ("percent", "per cent", "%") else [])
        hits.append(Hit(m.start(), m.end(), m.group(0), find_fact(cands, facts, prefer), cands))
    return sorted(hits, key=lambda h: h.start)


# ------------------------------------------------------------------ attribution hints

GENERIC = {"pct", "percent", "num", "count", "total", "avg", "sum", "value", "amt", "qty",
           "the", "per", "and", "mean", "median", "rate"}
CHANGE_WORDS = {"change", "delta", "growth", "diff"}


def row_labels(columns: list[str], rows: list[list[Any]]) -> dict[int, list[str]]:
    """Text cells per row (``North``, ``2026-08``), only when the result has several rows."""
    if len(rows) < 2:
        return {}
    return {i: [str(v) for v in r if isinstance(v, str) and v.strip()] for i, r in enumerate(rows)}


def _sentence(text: str, start: int) -> str:
    left = max((m.end() for m in SENTENCE_END_RE.finditer(text, 0, start)), default=0)
    m = SENTENCE_END_RE.search(text, start)
    return text[left:m.end() if m else len(text)]


def _names_column(col: str, sentence: str) -> bool:
    words = {w[:4] for w in re.findall(r"[a-z]+", sentence.lower())}
    toks = [t for t in col.lower().split("_") if len(t) >= 3 and t not in GENERIC]
    if not toks:
        return True
    if any(t in CHANGE_WORDS for t in toks) and DIRECTION_RE.search(sentence):
        return True
    return any(t[:4] in words for t in toks)


def attribution_warnings(text: str, hits: list[Hit],
                         labels: dict[int, list[str]] | None = None) -> list[str]:
    out = []
    for h in hits:
        if not h.fact_key or "[" not in h.fact_key:
            continue
        col, _, idx = h.fact_key.partition("[")
        sent = _sentence(text, h.start)
        if not _names_column(col, sent):
            out.append(f"{h.token} is {h.fact_key}, but '{col}' is not named in its sentence")
        row = (labels or {}).get(int(idx.rstrip("]")))
        if row and not any(lab.lower() in sent.lower() for lab in row):
            out.append(f"{h.token} is {h.fact_key}, but its row ({', '.join(row)}) "
                       "is not named in its sentence")
    return out


# ------------------------------------------------------------------ the check

def check_section(text: str, facts: dict[str, Any], dates_ok: list[int] | None = None,
                  numbers_used: list[dict[str, Any]] | None = None,
                  labels: dict[int, list[str]] | None = None) -> CheckResult:
    """Check one section. ``numbers_used=None`` skips citation enforcement (unit use only);
    the graph always passes the model's list, so an empty list means "nothing cited"."""
    bad, cited = [], []
    for ref in numbers_used or []:
        key, val = ref.get("fact_key"), ref.get("value")
        fv = facts.get(key) if isinstance(key, str) else None
        if fv is None or val is None or not close(float(val), float(fv)):
            bad.append(f"{key}={val}")
        else:
            cited.append(key)
    hits = scan(text, facts, dates_ok, prefer=cited)
    unsupported = [h.token for h in hits if h.fact_key is None]
    uncited: list[str] = []
    if numbers_used is not None:
        for h in hits:
            if h.fact_key is None:
                continue
            keys = [k for k in cited if matches(h.cands, facts[k])]
            if not keys:
                uncited.append(h.token)
                continue
            # Same value cited under several keys (4 incidents in two rows): take the key
            # whose column and row are named in this sentence, else the first.
            h.fact_key = next((k for k in keys if not attribution_warnings(
                text, [Hit(h.start, h.end, h.token, k)], labels)), keys[0])
    matched = {h.token: h.fact_key for h in hits if h.fact_key is not None}
    return CheckResult(ok=not (unsupported or uncited or bad), unsupported=unsupported,
                       matched=matched, bad_citations=bad, uncited=uncited,
                       warnings=attribution_warnings(text, hits, labels),
                       numbers_checked=len(hits))


def redact(text: str, facts: dict[str, Any], dates_ok: list[int] | None = None,
           mark: str = "[unverified]") -> tuple[str, list[str]]:
    """Replace every unsupported number with ``mark``; return (text, removed tokens)."""
    out, removed, pos = [], [], 0
    for h in scan(text, facts, dates_ok):
        if h.fact_key is None and h.start >= pos:
            out.append(text[pos:h.start])
            out.append(mark)
            removed.append(h.token)
            pos = h.end
    out.append(text[pos:])
    return "".join(out), removed
