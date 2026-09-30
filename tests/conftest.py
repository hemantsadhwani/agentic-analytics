"""Shared fixtures: SYNTHETIC sample databases built once per session, and a clean env.

No test reaches a real model: every provider here is ``FactsFakeProvider`` /
``FabricatingProvider`` / ``FakeProvider``, or a stub at the provider boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from samples import make_ops_db, make_sales_db

ROOT = Path(__file__).resolve().parent.parent
SALES_SPEC = ROOT / "specs" / "sales_mbr.yaml"
OPS_SPEC = ROOT / "specs" / "ops_weekly.yaml"


@pytest.fixture(scope="session")
def sales_db(tmp_path_factory) -> Path:
    return make_sales_db.build(tmp_path_factory.mktemp("data") / "sales.sqlite")


@pytest.fixture(scope="session")
def ops_db(tmp_path_factory) -> Path:
    return make_ops_db.build(tmp_path_factory.mktemp("data") / "ops.sqlite")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "REPORT_CHECKPOINT_DB",
                "REPORT_APPROVER", "LOGFIRE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
