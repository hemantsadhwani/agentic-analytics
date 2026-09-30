"""Spec loading and the first SQL lock: a single read-only SELECT, refused at load time."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_reporting.spec import UnsafeSQL, load_spec, spec_version, validate_sql
from conftest import OPS_SPEC, SALES_SPEC


def test_shipped_specs_load():
    sales, ops = load_spec(SALES_SPEC), load_spec(OPS_SPEC)
    assert [s.id for s in sales.sections] == ["summary", "revenue_by_region", "top_products",
                                              "month_on_month"]
    assert [s.id for s in ops.sections] == ["summary", "incidents_by_severity", "mttr",
                                            "top_services"]


def test_spec_version_is_sha256_of_bytes(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_bytes(SALES_SPEC.read_bytes())
    v1 = spec_version(p)
    assert len(v1) == 64 and v1 == spec_version(SALES_SPEC)
    p.write_bytes(SALES_SPEC.read_bytes() + b"\n# edited\n")
    assert spec_version(p) != v1


@pytest.mark.parametrize("sql", [
    "INSERT INTO orders VALUES (1)",
    "UPDATE orders SET revenue = 0",
    "DELETE FROM orders",
    "DROP TABLE orders",
    "SELECT 1; DROP TABLE orders",
    "SELECT 1; SELECT 2",
    "SELECT * FROM orders -- sneaky",
    "SELECT /* x */ 1",
    "WITH x AS (SELECT 1) UPDATE orders SET units = 0",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA writable_schema = 1",
    "",
])
def test_unsafe_sql_refused(sql):
    with pytest.raises(UnsafeSQL):
        validate_sql(sql)


def test_one_trailing_semicolon_is_allowed():
    assert validate_sql("SELECT 1;") == "SELECT 1"
    assert validate_sql("  WITH a AS (SELECT 1 AS n) SELECT n FROM a  ") .startswith("WITH")


def _write(tmp_path, body: str):
    p = tmp_path / "spec.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_spec_with_update_is_refused_at_load(tmp_path):
    p = _write(tmp_path, "title: t\nsections:\n  - id: a\n    heading: A\n"
                         "    sql: UPDATE orders SET units = 0\n    prompt: p\n")
    with pytest.raises(ValidationError, match="only SELECT"):
        load_spec(p)


def test_unknown_key_and_duplicate_ids_refused(tmp_path):
    typo = _write(tmp_path, "title: t\nsections:\n  - id: a\n    heading: A\n"
                            "    sql: SELECT 1\n    promt: p\n")
    with pytest.raises(ValidationError):
        load_spec(typo)
    dupes = _write(tmp_path, "title: t\nsections:\n"
                             "  - {id: a, heading: A, sql: SELECT 1, prompt: p}\n"
                             "  - {id: a, heading: B, sql: SELECT 2, prompt: p}\n")
    with pytest.raises(ValidationError, match="duplicate"):
        load_spec(dupes)
