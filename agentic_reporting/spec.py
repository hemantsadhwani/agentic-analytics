"""Report specs: a title and a list of sections, each one SELECT plus a drafting prompt.

    title: Monthly business review
    description: optional one line
    db: samples/sales.sqlite              # optional default data source; --db overrides
    sections:
      - id: revenue_by_region
        heading: Revenue by region
        sql: SELECT region, SUM(revenue) AS revenue FROM orders GROUP BY region
        prompt: Say which region led and how the others compare.
        max_words: 80

SQL is refused at load time unless it is a single ``SELECT`` (or ``WITH ... SELECT``)
statement. That is the first of three locks: ``data.py`` re-checks it, opens the database
with a read-only URI (``mode=ro``) and sets ``PRAGMA query_only``. The keyword check is
deliberately blunt: a string literal containing ``delete`` is refused too.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "DROP", "ATTACH", "DETACH", "PRAGMA", "CREATE",
             "ALTER", "REPLACE", "VACUUM", "REINDEX", "TRUNCATE", "GRANT")
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE)
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class UnsafeSQL(ValueError):
    """The SQL is not a single read-only SELECT."""


def validate_sql(sql: str) -> str:
    """Return ``sql`` normalised (one trailing ``;`` dropped) or raise ``UnsafeSQL``."""
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise UnsafeSQL("empty SQL")
    if ";" in s:
        raise UnsafeSQL("more than one statement (';' inside the SQL)")
    if "--" in s or "/*" in s:
        raise UnsafeSQL("SQL comments are not allowed in a report spec")
    first = s.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise UnsafeSQL(f"only SELECT is allowed, not {first}")
    bad = _FORBIDDEN_RE.search(s)
    if bad:
        raise UnsafeSQL(f"forbidden keyword {bad.group(1).upper()}")
    return s


Format = Literal["usd", "pct", "count", "pts"]


class ChartSpec(BaseModel):
    """A chart drawn by code from the section's own rows: every mark is a query value.

    ``line``: x is time, one line per y column. ``bar``: columns. ``hbar``: ranked horizontal
    bars (names on the left). ``style: emphasis`` draws the first y column in the accent and
    the others in a context gray (this year against last year); ``categorical`` gives each
    series its own hue.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["line", "bar", "hbar"]
    x: str
    y: list[str] = Field(min_length=1, max_length=3)
    labels: dict[str, str] = Field(default_factory=dict)     # column → legend name
    format: Format = "count"
    style: Literal["categorical", "emphasis"] = "categorical"
    caption: str | None = None


class TileSpec(BaseModel):
    """One headline number from the section's first row, with its changes (columns too)."""
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str                                   # column
    format: Format = "count"
    vs_prior: str | None = None                  # column holding the change vs last period
    vs_year: str | None = None                   # column holding the change vs a year ago
    change_format: Literal["pct", "pts"] = "pct"
    up_is_good: bool = True


class SectionSpec(BaseModel):
    """``kind: text`` (default) is prose; ``kpis`` adds headline tiles from the first row;
    ``decisions`` renders each row as an item needing attention (columns ``severity``:
    critical | warning | info, ``area``, ``issue``; the rule that picks the rows is the SQL)."""
    model_config = ConfigDict(extra="forbid")   # a typo in a key is an error, not ignored

    id: str
    heading: str
    sql: str
    prompt: str
    max_words: int = Field(default=120, ge=10, le=1000)
    kind: Literal["text", "kpis", "decisions"] = "text"
    chart: ChartSpec | None = None
    tiles: list[TileSpec] = Field(default_factory=list, max_length=6)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"section id {v!r} must be snake_case")
        return v

    @field_validator("sql")
    @classmethod
    def _sql(cls, v: str) -> str:
        return validate_sql(v)

    @model_validator(mode="after")
    def _kind(self) -> SectionSpec:
        if self.kind == "kpis" and not self.tiles:
            raise ValueError(f"section {self.id}: kind kpis needs tiles")
        if self.tiles and self.kind != "kpis":
            raise ValueError(f"section {self.id}: tiles need kind kpis")
        return self


class ReportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    db: str | None = None
    sections: list[SectionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> ReportSpec:
        ids = [s.id for s in self.sections]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate section ids: {dupes}")
        return self


def spec_version(path: str | Path) -> str:
    """``sha256`` of the spec file's bytes: the version recorded in the report and the audit."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_spec(path: str | Path) -> ReportSpec:
    """Load and validate a YAML spec (raises ``pydantic.ValidationError`` / ``UnsafeSQL``)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ReportSpec.model_validate(data)
