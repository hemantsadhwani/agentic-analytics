"""Time words → an exact date range, anchored on the data, not on today. No LLM.

    resolve("sales in the last 10 days", anchor=date(2014, 1, 28))
    # → TimeRange(start='2014-01-19', end='2014-01-28', phrase='last 10 days', ...)

**Why anchor on the data.** A warehouse is loaded up to some date (``data_as_of``: the latest
date in the metric's time column). "Last 10 days" asked of a sample that ends in January 2014
must mean the 10 days ending on that date, not the 10 days before today, which would return
nothing. The answer always states the range it used, so a stale warehouse is visible.

**Why resolve in code.** Date arithmetic is where models slip (off-by-one ranges, the wrong
quarter). Resolved here, the SQL writer is told the exact dates and the SQL is checked to
contain them.

Rules (documented, tested; a phrase not listed is left to the SQL writer with the anchor):

* ``last|past|previous|trailing N days`` → the N days ending on the anchor, inclusive;
  ``N weeks`` → 7·N days; ``N months`` → the N calendar months ending with the anchor's
  month; ``N years`` → the N calendar years ending with the anchor's year.
* ``last|latest|this|current week`` → the 7 days ending on the anchor; ``... month``,
  ``quarter``, ``year`` → that calendar period of the anchor, up to the anchor (so "last month"
  is the latest month in the data, possibly partial, and the answer says so).
* ``previous|prior month|quarter|year`` → the full calendar period before the anchor's.
* ``today`` / ``yesterday`` → the anchor day / the day before.
* ``Q3 2013`` / ``2013 Q3``; ``January 2013`` / ``Jan 2013``; a bare year ``2013``;
  several years ("2012 and 2013") → from the first to the last, whole years.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
             "thirty": 30, "ninety": 90}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS |= {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS["sept"] = 9

_N = r"(\d{1,3}|" + "|".join(NUM_WORDS) + r")"
ROLLING_RE = re.compile(
    rf"\b(?:last|past|previous|trailing)\s+{_N}\s+(day|week|month|quarter|year)s?\b")
CURRENT_RE = re.compile(r"\b(last|latest|this|current|previous|prior)\s+(week|month|quarter|year)\b")
DAY_RE = re.compile(r"\b(today|yesterday)\b")
QUARTER_RE = re.compile(r"\bq([1-4])\s*(?:of\s+)?(20\d\d)\b|\b(20\d\d)\s*q([1-4])\b")
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True))
                      + r")\.?\s+(?:of\s+)?(20\d\d)\b")
YEAR_RE = re.compile(r"\b(20\d\d)\b")


@dataclass
class TimeRange:
    start: str
    end: str
    phrase: str                 # the words of the question it came from
    rule: str                   # which documented rule produced it
    column: str | None = None   # the metric's time column, set by the caller

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def label(self) -> str:
        return self.start if self.start == self.end else f"{self.start} to {self.end}"


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else NUM_WORDS[tok]


def _month_start(d: date, back: int = 0) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - back, 12)
    return date(y, m + 1, 1)


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _range(start: date, end: date, phrase: str, rule: str) -> TimeRange:
    return TimeRange(start.isoformat(), end.isoformat(), phrase, rule)


def resolve(question: str, anchor: date) -> TimeRange | None:
    """The first rule that matches ``question`` (lower-cased), or None."""
    q = question.lower()
    if m := ROLLING_RE.search(q):
        n, unit = _n(m.group(1)), m.group(2)
        if n < 1:
            return None
        if unit == "day":
            start = anchor - timedelta(days=n - 1)
        elif unit == "week":
            start = anchor - timedelta(days=7 * n - 1)
        elif unit == "month":
            start = _month_start(anchor, n - 1)
        elif unit == "quarter":
            start = _month_start(_quarter_start(anchor), 3 * (n - 1))
        else:
            start = date(anchor.year - n + 1, 1, 1)
        return _range(start, anchor, m.group(0), f"rolling {n} {unit}(s) ending on the anchor")
    if m := CURRENT_RE.search(q):
        which, unit = m.groups()
        if which in ("previous", "prior"):
            if unit == "week":
                end = anchor - timedelta(days=7)
                return _range(end - timedelta(days=6), end, m.group(0),
                              "the 7 days before the last 7")
            if unit == "month":
                s = _month_start(anchor, 1)
                return _range(s, _month_end(s), m.group(0),
                              "the calendar month before the anchor's")
            if unit == "quarter":
                s = _month_start(_quarter_start(anchor), 3)
                return _range(s, _month_end(_month_start(s, -2)), m.group(0),
                              "the calendar quarter before the anchor's")
            return _range(date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31), m.group(0),
                          "the calendar year before the anchor's")
        if unit == "week":
            return _range(anchor - timedelta(days=6), anchor, m.group(0),
                          "the 7 days ending on the anchor")
        if unit == "month":
            return _range(_month_start(anchor), anchor, m.group(0),
                          "the anchor's calendar month, up to the anchor")
        if unit == "quarter":
            return _range(_quarter_start(anchor), anchor, m.group(0),
                          "the anchor's calendar quarter, up to the anchor")
        return _range(date(anchor.year, 1, 1), anchor, m.group(0),
                      "the anchor's calendar year, up to the anchor")
    if m := DAY_RE.search(q):
        d = anchor if m.group(1) == "today" else anchor - timedelta(days=1)
        rule = "the anchor day" if m.group(1) == "today" else "the day before the anchor"
        return _range(d, d, m.group(0), rule)
    if m := QUARTER_RE.search(q):
        qn, y = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        s = date(int(y), 3 * (int(qn) - 1) + 1, 1)
        return _range(s, _month_end(_month_start(s, -2)), m.group(0), "a calendar quarter")
    if m := MONTH_RE.search(q):
        s = date(int(m.group(2)), MONTHS[m.group(1)], 1)
        return _range(s, _month_end(s), m.group(0), "a calendar month")
    years = sorted({int(y) for y in YEAR_RE.findall(q)})
    if years:
        phrase = " and ".join(str(y) for y in years)
        return _range(date(years[0], 1, 1), date(years[-1], 12, 31), phrase,
                      "whole calendar year(s)")
    return None
