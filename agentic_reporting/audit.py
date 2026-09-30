"""The audit log: one row per report decision, in the checkpoint SQLite file.

A row is written when a report is published (approved at the gate, or auto-published with
``--no-gate``) and when it is rejected. It records what was decided, by whom, on which spec
version, with which model, at what cost, and how many numbers the checker verified.

``thread`` is the primary key and the insert is ``INSERT OR IGNORE``: LangGraph may replay a
node after a crash, and a replay must not write a second row for the same decision.
Stdlib ``sqlite3`` only.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COLUMNS = ("thread", "spec", "spec_version", "provider", "model", "usd", "llm_calls",
           "sections", "numbers_checked", "unverified", "decision", "decided_by",
           "published_path", "created_at")

DDL = """
CREATE TABLE IF NOT EXISTS audit (
    thread          TEXT PRIMARY KEY,
    spec            TEXT NOT NULL,
    spec_version    TEXT NOT NULL,      -- sha256 of the spec YAML bytes
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    usd             REAL NOT NULL,
    llm_calls       INTEGER NOT NULL,
    sections        INTEGER NOT NULL,
    numbers_checked INTEGER NOT NULL,
    unverified      INTEGER NOT NULL,
    decision        TEXT NOT NULL,      -- approved | rejected | auto (--no-gate)
    decided_by      TEXT NOT NULL,
    published_path  TEXT,
    created_at      TEXT NOT NULL
)
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute(DDL)
    return conn


def record(db_path: str | Path, row: dict[str, Any]) -> bool:
    """Insert one audit row. Returns False if this thread already has one (a replay)."""
    values = {**row, "created_at": row.get("created_at")
              or datetime.now(UTC).isoformat(timespec="seconds")}
    missing = [c for c in COLUMNS if c not in values and c != "published_path"]
    if missing:
        raise ValueError(f"audit row missing {missing}")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO audit ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                [values.get(c) for c in COLUMNS])
        return cur.rowcount == 1
    finally:
        conn.close()


def recent(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Newest audit rows first. An absent file or table means no rows."""
    if not Path(db_path).is_file():
        return []
    conn = _connect(db_path)
    try:
        cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM audit "
                           "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [dict(zip(COLUMNS, r, strict=True)) for r in cur.fetchall()]
    finally:
        conn.close()
