"""The public demo's API: one question in, one checked answer out. Lambda or local.

    POST /api/ask      {"question": "...", "metric": "<name>"?}  ->  the answer as JSON
    GET  /api/health   ->  {"ok", "enabled", "today": {"calls", "spent_usd", ...}}

    python -m agentic_reporting.demo_api --serve                 # http://127.0.0.1:8080, fake model
    python -m agentic_reporting.demo_api --serve --provider bedrock-converse   # real Bedrock

The same ``handle`` runs behind API Gateway (``lambda_handler``) and in the local server, so
what is tested locally is what is deployed.

**Guards, in the order a request meets them** (API Gateway throttling comes first, outside):

1. **Kill switch.** ``DEMO_ENABLED`` must be ``true``; otherwise every question gets 503.
2. **Input.** A question of 1 to 300 characters, no control characters; an optional metric
   name (the reply to a clarifying question) that must be a governed metric.
3. **Daily budget.** One atomic counter per UTC day: a call is admitted only while today's
   calls are under ``DAILY_CALL_CAP`` and today's spend under ``DAILY_USD_CAP``. The
   admission is one conditional write, so parallel requests cannot overshoot the call cap;
   the dollar cap can be passed by at most the requests already in flight. In AWS the
   counter is a DynamoDB item; locally it is in memory.
4. **The chatbot's own checks** (ADR-0006): read-only SQL over linked tables, filters checked,
   numbers checked, documents cited.

The answer returned is the chatbot's answer, trimmed for the page (at most 50 rows). Each
request logs one JSON line (no IP address, the question cut to 300 characters).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "examples" / "aw_monthly_2013-11.html"   # published as /report.html
MAX_QUESTION = 300
MAX_ROWS = 50
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# --- budget -------------------------------------------------------------------------------

def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class MemoryBudget:
    """Local stand-in for the DynamoDB counter."""

    def __init__(self, call_cap: int, usd_cap: float) -> None:
        self.call_cap, self.usd_cap = call_cap, usd_cap
        self.days: dict[str, dict[str, float]] = {}
        self.lock = threading.Lock()

    def admit(self) -> bool:
        with self.lock:
            d = self.days.setdefault(today(), {"calls": 0, "spent_usd": 0.0})
            if d["calls"] >= self.call_cap or d["spent_usd"] >= self.usd_cap:
                return False
            d["calls"] += 1
            return True

    def spend(self, usd: float) -> None:
        with self.lock:
            self.days.setdefault(today(), {"calls": 0, "spent_usd": 0.0})["spent_usd"] += usd

    def status(self) -> dict[str, Any]:
        d = self.days.get(today(), {"calls": 0, "spent_usd": 0.0})
        return {"day": today(), "calls": int(d["calls"]), "spent_usd": round(d["spent_usd"], 6),
                "call_cap": self.call_cap, "usd_cap": self.usd_cap}


class DynamoBudget:
    """One item per UTC day: ``{day, calls, spent_usd, expires}``; expired by TTL after 30 days."""

    def __init__(self, table: str, call_cap: int, usd_cap: float, client: Any = None) -> None:
        self.table, self.call_cap, self.usd_cap = table, call_cap, usd_cap
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def admit(self) -> bool:
        expires = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
        try:
            self.client.update_item(
                TableName=self.table, Key={"day": {"S": today()}},
                UpdateExpression="ADD calls :one SET expires = :exp",
                ConditionExpression="attribute_not_exists(calls) OR "
                                    "(calls < :cap AND (attribute_not_exists(spent_usd) "
                                    "OR spent_usd < :usd))",
                ExpressionAttributeValues={":one": {"N": "1"}, ":cap": {"N": str(self.call_cap)},
                                           ":usd": {"N": str(self.usd_cap)},
                                           ":exp": {"N": str(expires)}})
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            return False

    def spend(self, usd: float) -> None:
        if usd > 0:
            self.client.update_item(
                TableName=self.table, Key={"day": {"S": today()}},
                UpdateExpression="ADD spent_usd :c",
                ExpressionAttributeValues={":c": {"N": f"{usd:.8f}"}})

    def status(self) -> dict[str, Any]:
        item = self.client.get_item(TableName=self.table, Key={"day": {"S": today()}},
                                    ConsistentRead=True).get("Item", {})
        return {"day": today(), "calls": int(item.get("calls", {}).get("N", 0)),
                "spent_usd": round(float(item.get("spent_usd", {}).get("N", 0)), 6),
                "call_cap": self.call_cap, "usd_cap": self.usd_cap}


# --- the app ------------------------------------------------------------------------------

class Demo:
    """Everything a request needs, built once per process (one Lambda cold start)."""

    def __init__(self, db: Path, provider_name: str, embedder: str, budget: Any,
                 enabled: bool = True, workdir: Path | None = None) -> None:
        from agentic_reporting.chat import documents_for, get_providers, knowledge
        self.enabled, self.budget = enabled, budget
        work = workdir or Path(os.environ.get("DEMO_WORKDIR", ROOT / ".agentic_reporting"))
        self.db = db
        self.sql_p, self.ans_p = get_providers(provider_name, db)
        if provider_name == "fake":
            from agentic_reporting.rag import FakeRagProvider
            doc_p: Any = FakeRagProvider()
        else:
            doc_p = self.sql_p
        self.docs = documents_for(db, "retrieve", doc_p, embedder, work)
        self.graph, _ = knowledge(db)
        self.metrics = {d["name"] for _, d in self.graph.nodes(data=True) if d.get("kind") == "metric"}
        self.label = f"{getattr(self.sql_p, 'name', '?')}/{getattr(self.sql_p, 'model', '?')}"

    def health(self) -> tuple[int, dict[str, Any]]:
        try:
            b = self.budget.status()
        except Exception as e:  # noqa: BLE001  health must answer even if the counter is down
            b = {"error": type(e).__name__}
        return 200, {"ok": True, "enabled": self.enabled, "model": self.label, "today": b}

    def ask(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self.enabled:
            return 503, {"error": "paused", "message": "The demo is paused by its owner."}
        q = body.get("question")
        if not isinstance(q, str) or not q.strip():
            return 400, {"error": "bad_request", "message": "Type a question."}
        q = " ".join(q.split())
        if len(q) > MAX_QUESTION or _CONTROL.search(q):
            return 400, {"error": "bad_request",
                         "message": f"Keep the question under {MAX_QUESTION} characters."}
        metric = body.get("metric")
        if metric is not None and (not isinstance(metric, str) or not _METRIC.match(metric)
                                   or (self.metrics and metric not in self.metrics)):
            return 400, {"error": "bad_request", "message": "Unknown metric."}
        if not self.budget.admit():
            return 429, {"error": "budget", "message": "Today's demo budget is used up. "
                         "It resets at midnight UTC.", "today": self.budget.status()}
        from agentic_reporting.chat import ask
        t0 = time.perf_counter()
        a = ask(q, self.db, self.sql_p, self.ans_p, metric=metric, documents=self.docs)
        try:
            self.budget.spend(a.cost_usd)
        except Exception as e:  # noqa: BLE001  the answer is already paid for; log, don't fail
            log({"event": "budget_spend_failed", "error": type(e).__name__})
        out = public(a)
        log({"event": "ask", "status": a.status, "route": a.route, "chars": len(q),
             "question": q[:MAX_QUESTION], "cost_usd": round(a.cost_usd, 7),
             "latency_ms": int((time.perf_counter() - t0) * 1000), "model": a.model})
        return 200, out


def public(a: Any) -> dict[str, Any]:
    """The answer, trimmed for the page."""
    d = a.to_dict()
    d["rows"] = d["rows"][:MAX_ROWS]
    d["rows_truncated"] = len(a.rows) > MAX_ROWS or a.truncated
    chk = a.check or {}
    d["check"] = {"ok": chk.get("ok"), "numbers_checked": chk.get("numbers_checked"),
                  "unsupported": chk.get("unsupported", [])} if chk else None
    for k in ("first_sql", "truncated"):
        d.pop(k, None)
    return d


def log(rec: dict[str, Any]) -> None:
    print(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), **rec},
                     default=str), flush=True)


# --- Lambda -------------------------------------------------------------------------------

_DEMO: Demo | None = None


def _cold_start() -> Demo:
    """Copy the read-only package files to /tmp (SQLite and the index write small files)."""
    tmp = Path(os.environ.get("DEMO_WORKDIR", "/tmp/demo"))
    tmp.mkdir(parents=True, exist_ok=True)
    src_db = ROOT / "samples" / "adventureworks.sqlite"
    db = tmp / src_db.name
    if not db.exists():
        shutil.copy2(src_db, db)
    embedder = os.environ.get("EMBEDDER", "titan")
    idx_name = f"docs_{db.stem}_{embedder}.sqlite"
    src_idx = ROOT / ".agentic_reporting" / idx_name
    if src_idx.exists() and not (tmp / idx_name).exists():
        shutil.copy2(src_idx, tmp / idx_name)
    table = os.environ.get("BUDGET_TABLE")
    calls, usd = int(os.environ.get("DAILY_CALL_CAP", "300")), float(os.environ.get("DAILY_USD_CAP", "1.0"))
    budget = DynamoBudget(table, calls, usd) if table else MemoryBudget(calls, usd)
    return Demo(db, os.environ.get("LLM_PROVIDER", "bedrock-converse"), embedder, budget,
                enabled=os.environ.get("DEMO_ENABLED", "false").lower() == "true", workdir=tmp)


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """API Gateway HTTP API (payload 2.0)."""
    global _DEMO
    secret = os.environ.get("ORIGIN_SECRET")
    if secret:   # only CloudFront knows it: calling API Gateway directly skips the page's limits
        import hmac
        got = (event.get("headers") or {}).get("x-origin-verify", "")
        if not hmac.compare_digest(got.encode(), secret.encode()):
            return {"statusCode": 403, "headers": {"content-type": "application/json"},
                    "body": json.dumps({"error": "forbidden"})}
    if _DEMO is None:
        _DEMO = _cold_start()
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "")
    if method == "GET" and path.endswith("/health"):
        code, body = _DEMO.health()
    elif method == "POST" and path.endswith("/ask"):
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        if len(raw) > 4096:
            code, body = 413, {"error": "too_large", "message": "Request too large."}
        else:
            try:
                parsed = json.loads(raw)
                code, body = _DEMO.ask(parsed if isinstance(parsed, dict) else {})
            except json.JSONDecodeError:
                code, body = 400, {"error": "bad_request", "message": "Send JSON."}
    else:
        code, body = 404, {"error": "not_found"}
    return {"statusCode": code, "headers": {"content-type": "application/json",
                                            "cache-control": "no-store"},
            "body": json.dumps(body, default=str)}


# --- local server -------------------------------------------------------------------------

def serve(demo: Demo, site: Path, port: int) -> None:
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(site), **kw)

        def _json(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/health"):
                self._json(*demo.health())
            elif self.path.split("?")[0] == "/report.html":     # one copy: docs/examples
                data = REPORT.read_bytes()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/ask"):
                self._json(404, {"error": "not_found"})
                return
            n = int(self.headers.get("content-length") or 0)
            if n > 4096:
                self._json(413, {"error": "too_large"})
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad_request", "message": "Send JSON."})
                return
            self._json(*demo.ask(body if isinstance(body, dict) else {}))

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"demo at http://127.0.0.1:{port}  (model {demo.label}; Ctrl+C to stop)")
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--provider", default="fake")
    ap.add_argument("--embedder", default=None, help="default: fake with --provider fake, else titan")
    ap.add_argument("--db", default=str(ROOT / "samples" / "adventureworks.sqlite"))
    ap.add_argument("--site", default=str(ROOT / "demo" / "site"))
    args = ap.parse_args(argv)
    if not args.serve:
        ap.print_help()
        return 0
    embedder = args.embedder or ("fake" if args.provider == "fake" else "titan")
    demo = Demo(Path(args.db), args.provider, embedder, MemoryBudget(300, 1.0))
    serve(demo, Path(args.site), args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
