"""The data layer: read-only by three locks, row-capped, numbers flattened into facts."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agentic_reporting.data import connect_ro, run_query
from agentic_reporting.spec import UnsafeSQL


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_update_fails_and_file_is_unchanged(sales_db):
    before = _sha(sales_db)
    with pytest.raises(UnsafeSQL):
        run_query(sales_db, "UPDATE orders SET revenue = 0")
    conn = connect_ro(sales_db)          # even past the SQL check, SQLite refuses
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE orders SET revenue = 0")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil (x)")
    finally:
        conn.close()
    assert _sha(sales_db) == before


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError):
        run_query(missing, "SELECT 1")
    assert not missing.exists()


def test_row_cap_and_truncated_flag(sales_db):
    q = run_query(sales_db, "SELECT order_id, revenue FROM orders ORDER BY order_id", row_cap=10)
    assert len(q.rows) == 10 and q.truncated is True
    assert q.facts["order_id[0]"] == 1 and "order_id[10]" not in q.facts
    small = run_query(sales_db, "SELECT COUNT(*) AS n FROM orders", row_cap=10)
    assert small.truncated is False and small.facts == {"n[0]": small.rows[0][0]}


def test_facts_skip_text_and_null_and_collect_years(sales_db):
    q = run_query(sales_db, "SELECT order_date, region, units, NULL AS empty_col "
                            "FROM orders ORDER BY order_id LIMIT 2")
    assert set(q.facts) == {"units[0]", "units[1]"}
    assert q.dates_ok == [2026]
