"""Run one spec query against SQLite, read-only, and flatten the numeric cells into facts.

Three locks keep the query from writing: ``validate_sql`` (single SELECT), a read-only
URI (``file:...?mode=ro``, so SQLite itself refuses writes and never creates a missing
file), and ``PRAGMA query_only = ON``. ``sqlite3.execute`` also refuses a second statement.

``facts`` is the only thing the checker trusts: ``{"<column>[<row_index>]": number}`` for
every int/float cell (bools, NULLs, text, NaN and inf excluded). Text cells that look like
dates contribute their years to ``dates_ok`` so "in 2026" is not flagged as a made-up number.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentic_reporting.spec import validate_sql

ROW_CAP = 200
DATE_RE = re.compile(r"^(\d{4})-\d{2}(?:-\d{2})?(?:[T ][0-9:.+\-Z]*)?$")


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    facts: dict[str, float] = field(default_factory=dict)
    truncated: bool = False
    dates_ok: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_number(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _cell(v: Any) -> Any:
    """JSON-safe cell: bytes become a short hex string, non-finite floats become None."""
    if isinstance(v, bytes):
        return v[:16].hex()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def facts_from(columns: list[str], rows: list[list[Any]]) -> dict[str, float]:
    return {f"{col}[{i}]": val for i, row in enumerate(rows)
            for col, val in zip(columns, row, strict=True) if is_number(val)}


def years_from(rows: list[list[Any]]) -> list[int]:
    years = {int(m.group(1)) for row in rows for v in row
             if isinstance(v, str) and (m := DATE_RE.match(v.strip()))}
    return sorted(years)


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no SQLite database at {db_path}")
    conn = sqlite3.connect(f"file:{quote(p.as_posix())}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def only_tables(allowed: set[str] | frozenset[str]):
    """A SQLite authorizer: plain SELECT reads of ``allowed`` tables and function calls only.

    SQLite reports every column read against the real table (CTEs and subqueries resolved),
    so a query that touches any other table, or tries anything but a read, fails to prepare.
    """
    allow = {t.lower() for t in allowed}
    ok_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def authorize(action: int, arg1: str | None, arg2: str | None, db: str | None,
                  trigger: str | None) -> int:
        if action in ok_actions:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and (arg1 or "").lower() in allow:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def run_query(db_path: str | Path, sql: str, row_cap: int = ROW_CAP,
              allowed_tables: set[str] | frozenset[str] | None = None) -> QueryResult:
    """``allowed_tables``, if given, is a fourth lock: SQLite refuses reads of any other table."""
    sql = validate_sql(sql)
    conn = connect_ro(db_path)
    try:
        if allowed_tables is not None:
            conn.set_authorizer(only_tables(allowed_tables))
        cur = conn.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(row_cap + 1)
    finally:
        conn.close()
    truncated = len(fetched) > row_cap
    rows = [[_cell(v) for v in r] for r in fetched[:row_cap]]
    return QueryResult(columns=columns, rows=rows, facts=facts_from(columns, rows),
                       truncated=truncated, dates_ok=years_from(rows))
