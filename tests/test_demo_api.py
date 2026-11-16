"""The public demo API's guards, offline: kill switch, input limits, daily budget, the
CloudFront secret header, and the Lambda event shape. Uses the fake model and the tiny
AdventureWorks-shaped warehouse from test_chat."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_chat import SCHEMA

from agentic_reporting import demo_api
from agentic_reporting.demo_api import Demo, DynamoBudget, MemoryBudget


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "aw.sqlite"
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return p


def make(db: Path, tmp_path: Path, enabled: bool = True, calls: int = 10) -> Demo:
    return Demo(db, "fake", "fake", MemoryBudget(calls, 1.0), enabled=enabled,
                workdir=tmp_path / "work")


def test_input_is_checked_before_anything_is_spent(db: Path, tmp_path: Path) -> None:
    d = make(db, tmp_path)
    assert d.ask({"question": ""})[0] == 400
    assert d.ask({"question": "x" * 301})[0] == 400
    assert d.ask({"question": "sales\x00"})[0] == 400
    assert d.ask({"question": "sales", "metric": "x'; DROP"})[0] == 400
    assert d.ask({"question": "sales", "metric": "not_a_metric"})[0] == 400
    assert d.budget.status()["calls"] == 0


def test_kill_switch(db: Path, tmp_path: Path) -> None:
    code, body = make(db, tmp_path, enabled=False).ask({"question": "Internet sales in 2013"})
    assert code == 503 and body["error"] == "paused"


def test_daily_call_cap(db: Path, tmp_path: Path) -> None:
    d = make(db, tmp_path, calls=2)
    assert d.ask({"question": "Internet sales in 2013"})[0] == 200
    assert d.ask({"question": "Internet sales in 2013"})[0] == 200
    code, body = d.ask({"question": "Internet sales in 2013"})
    assert code == 429 and body["error"] == "budget"


def test_dollar_cap() -> None:
    b = MemoryBudget(100, 0.001)
    assert b.admit()
    b.spend(0.002)
    assert not b.admit()


def test_answer_is_trimmed_for_the_page(db: Path, tmp_path: Path) -> None:
    code, a = make(db, tmp_path).ask({"question": "Internet sales by sales territory group in Q3 2013"})
    assert code == 200 and "first_sql" not in a and "rows_truncated" in a


class FakeDynamo:
    """Just enough of the DynamoDB client for the conditional counter."""

    class exceptions:  # noqa: N801
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self) -> None:
        self.item: dict[str, float] = {}

    def update_item(self, **kw: object) -> None:
        vals = kw["ExpressionAttributeValues"]
        if "ConditionExpression" in kw:
            cap, usd = float(vals[":cap"]["N"]), float(vals[":usd"]["N"])
            if self.item and not (self.item.get("calls", 0) < cap and self.item.get("spent_usd", 0) < usd):
                raise self.exceptions.ConditionalCheckFailedException()
            self.item["calls"] = self.item.get("calls", 0) + 1
        else:
            self.item["spent_usd"] = self.item.get("spent_usd", 0) + float(vals[":c"]["N"])

    def get_item(self, **kw: object) -> dict:
        return {"Item": {k: {"N": str(v)} for k, v in self.item.items()}}


def test_dynamo_budget_admits_until_a_cap() -> None:
    b = DynamoBudget("t", 2, 1.0, client=FakeDynamo())
    assert b.admit() and b.admit() and not b.admit()
    b2 = DynamoBudget("t", 100, 0.01, client=FakeDynamo())
    assert b2.admit()
    b2.spend(0.02)
    assert not b2.admit() and b2.status()["spent_usd"] == pytest.approx(0.02)


def event(method: str, path: str, body: dict | None = None, secret: str | None = None) -> dict:
    return {"rawPath": path, "requestContext": {"http": {"method": method}},
            "headers": {"x-origin-verify": secret} if secret else {},
            "body": json.dumps(body) if body is not None else None}


def test_lambda_requires_the_cloudfront_header(db: Path, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORIGIN_SECRET", "s3cret")
    monkeypatch.setattr(demo_api, "_DEMO", make(db, tmp_path))
    assert demo_api.lambda_handler(event("GET", "/api/health"))["statusCode"] == 403
    assert demo_api.lambda_handler(event("GET", "/api/health", secret="wrong"))["statusCode"] == 403
    ok = demo_api.lambda_handler(event("GET", "/api/health", secret="s3cret"))
    assert ok["statusCode"] == 200 and json.loads(ok["body"])["ok"]
    r = demo_api.lambda_handler(event("POST", "/api/ask", {"question": "Internet sales in 2013"},
                                      secret="s3cret"))
    assert r["statusCode"] == 200 and r["headers"]["cache-control"] == "no-store"
    assert demo_api.lambda_handler(event("POST", "/api/nope", {}, secret="s3cret"))["statusCode"] == 404
    bad = {**event("POST", "/api/ask", None, secret="s3cret"), "body": "{not json"}
    assert demo_api.lambda_handler(bad)["statusCode"] == 400
