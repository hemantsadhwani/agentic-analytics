"""The chatbot path on a tiny AdventureWorks-shaped database (offline, no download).

Covers: the time resolver, the value index, the linker's star-schema rules (CamelCase, *Key,
one-way joins, ambiguity), the chat pipeline's ask-back / refuse / repair behaviour, the
checker's name masking, and the hash-chained audit log.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentic_reporting.chat import ask, template_answer
from agentic_reporting.check import check_section
from agentic_reporting.providers.base import LLMResponse
from agentic_reporting.providers.fake import FactsFakeProvider
from agentic_reporting.schema_graph import build_schema_graph, is_key, link, tokens
from agentic_reporting.sql_writer import SQLAnswer
from agentic_reporting.timeframe import resolve
from agentic_reporting.values import add_to_graph, build_value_index

SCHEMA = """
CREATE TABLE DimSalesTerritory (SalesTerritoryKey INTEGER PRIMARY KEY, SalesTerritoryRegion TEXT,
  SalesTerritoryCountry TEXT, SalesTerritoryGroup TEXT);
CREATE TABLE DimProduct (ProductKey INTEGER PRIMARY KEY, EnglishProductName TEXT, ModelName TEXT,
  Color TEXT);
CREATE TABLE DimGeography (GeographyKey INTEGER PRIMARY KEY, StateProvinceName TEXT,
  SalesTerritoryKey INTEGER REFERENCES DimSalesTerritory(SalesTerritoryKey));
CREATE TABLE DimCustomer (CustomerKey INTEGER PRIMARY KEY,
  GeographyKey INTEGER REFERENCES DimGeography(GeographyKey));
CREATE TABLE DimReseller (ResellerKey INTEGER PRIMARY KEY, ResellerName TEXT, BusinessType TEXT,
  GeographyKey INTEGER REFERENCES DimGeography(GeographyKey));
CREATE TABLE FactInternetSales (ProductKey INTEGER REFERENCES DimProduct(ProductKey),
  CustomerKey INTEGER REFERENCES DimCustomer(CustomerKey),
  SalesTerritoryKey INTEGER REFERENCES DimSalesTerritory(SalesTerritoryKey),
  SalesOrderNumber TEXT, OrderQuantity INTEGER, SalesAmount REAL, TotalProductCost REAL,
  OrderDate TEXT);
CREATE TABLE FactResellerSales (ProductKey INTEGER REFERENCES DimProduct(ProductKey),
  ResellerKey INTEGER REFERENCES DimReseller(ResellerKey),
  SalesTerritoryKey INTEGER REFERENCES DimSalesTerritory(SalesTerritoryKey),
  SalesOrderNumber TEXT, OrderQuantity INTEGER, SalesAmount REAL, TotalProductCost REAL,
  OrderDate TEXT);
INSERT INTO DimSalesTerritory VALUES (1,'Northwest','United States','North America'),
  (2,'Canada','Canada','North America'), (3,'France','France','Europe');
INSERT INTO DimProduct VALUES (1,'Mountain-200 Black, 38','Mountain-200','Black'),
  (2,'Road-150 Red, 44','Road-150','Red');
INSERT INTO DimGeography VALUES (1,'Washington',1),(2,'Ontario',2);
INSERT INTO DimCustomer VALUES (1,1),(2,2);
INSERT INTO DimReseller VALUES (1,'Bike World','Warehouse',1);
INSERT INTO FactInternetSales VALUES (1,1,1,'SO1',1,100.0,60.0,'2014-01-20'),
  (1,1,1,'SO2',2,200.0,120.0,'2014-01-28'), (2,2,2,'SO3',1,50.0,30.0,'2014-01-10'),
  (2,2,3,'SO4',1,70.0,40.0,'2013-12-15');
INSERT INTO FactResellerSales VALUES (1,1,1,'SO9',5,500.0,450.0,'2013-11-29');
"""


@pytest.fixture
def aw(tmp_path: Path) -> Path:
    db = tmp_path / "aw.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


class Scripted:
    """Answers the SQL prompt with the given SQL, one per call; records every call."""

    name, model = "fake", "scripted-sql-1"

    def __init__(self, *sqls: str, metric: str = "internet_sales") -> None:
        self.sqls, self.metric, self.calls = list(sqls), metric, []

    def complete(self, **kw: Any) -> LLMResponse:
        self.calls.append(kw)
        sql = self.sqls.pop(0) if len(self.sqls) > 1 else self.sqls[0]
        a = SQLAnswer(sql=sql, uses_metrics=[self.metric], rationale="test")
        return LLMResponse(text=a.model_dump_json(), parsed=a, provider=self.name,
                           model=self.model, input_tokens=10, output_tokens=5, cost_usd=0.0,
                           structured_mode="native")


NW_10 = ("SELECT ROUND(SUM(FactInternetSales.SalesAmount), 2) AS internet_sales "
         "FROM FactInternetSales JOIN DimProduct ON FactInternetSales.ProductKey = "
         "DimProduct.ProductKey JOIN DimSalesTerritory ON FactInternetSales.SalesTerritoryKey = "
         "DimSalesTerritory.SalesTerritoryKey WHERE DimProduct.EnglishProductName = "
         "'Mountain-200 Black, 38' AND DimSalesTerritory.SalesTerritoryRegion = 'Northwest' "
         "AND FactInternetSales.OrderDate BETWEEN '2014-01-19' AND '2014-01-28'")
Q_NW = "What were internet sales of Mountain-200 Black, 38 in the Northwest in the last 10 days?"


# --- time ---------------------------------------------------------------------------------

@pytest.mark.parametrize("q, start, end", [
    ("sales in the last 10 days", "2014-01-19", "2014-01-28"),
    ("past two weeks", "2014-01-15", "2014-01-28"),
    ("last 3 months", "2013-11-01", "2014-01-28"),
    ("last month", "2014-01-01", "2014-01-28"),
    ("previous month", "2013-12-01", "2013-12-31"),
    ("last quarter", "2014-01-01", "2014-01-28"),
    ("prior quarter", "2013-10-01", "2013-12-31"),
    ("this year", "2014-01-01", "2014-01-28"),
    ("previous year", "2013-01-01", "2013-12-31"),
    ("in Q3 2013", "2013-07-01", "2013-09-30"),
    ("2012 q4", "2012-10-01", "2012-12-31"),
    ("in February 2012", "2012-02-01", "2012-02-29"),
    ("in 2012 and 2013", "2012-01-01", "2013-12-31"),
    ("yesterday", "2014-01-27", "2014-01-27"),
])
def test_time_words_resolve_against_the_data_not_today(q: str, start: str, end: str) -> None:
    tr = resolve(q, date(2014, 1, 28))
    assert tr is not None and (tr.start, tr.end) == (start, end)


def test_no_time_words_resolve_to_nothing() -> None:
    assert resolve("internet sales by region", date(2014, 1, 28)) is None


# --- linker on a star schema --------------------------------------------------------------

def test_camelcase_and_key_columns() -> None:
    assert tokens("EnglishProductName") == ["english", "product", "name"]
    assert tokens("order_date") == ["order", "date"]
    assert is_key("ProductKey") and is_key("customer_id") and not is_key("SalesAmount")


def test_joins_go_from_fact_to_dimension_only(aw: Path) -> None:
    g = build_schema_graph(aw)
    lr = link("internet sales by reseller", g)
    assert "DimReseller" in lr.unreachable            # fan trap: refused, not joined
    lr = link("internet sales by sales territory group", g)
    assert lr.tables == ["FactInternetSales", "DimSalesTerritory"] and not lr.unreachable


def test_a_word_with_two_governed_meanings_asks(aw: Path) -> None:
    g = build_schema_graph(aw)
    lr = link("sales last month", g)
    assert lr.clarify and {o["metric"] for o in lr.clarify["options"]} == {
        "internet_sales", "reseller_sales"}
    assert not link("internet sales last month", g).clarify
    assert link("sales last month", g, metric="reseller_sales").metrics[0] == "reseller_sales"


# --- value index --------------------------------------------------------------------------

def test_longest_value_wins_and_alternatives_are_kept(aw: Path) -> None:
    g = build_schema_graph(aw)
    idx = build_value_index(aw, g)
    found = {(m.column, m.value) for m in idx.match(Q_NW)}
    assert found == {("DimProduct.EnglishProductName", "Mountain-200 Black, 38"),
                     ("DimSalesTerritory.SalesTerritoryRegion", "Northwest")}
    found = {(m.column, m.value) for m in idx.match("mountain-200 black sales")}
    assert found == {("DimProduct.ModelName", "Mountain-200"), ("DimProduct.Color", "Black")}
    canada = idx.match("sales in canada")[0]
    assert canada.column == "DimSalesTerritory.SalesTerritoryRegion"
    assert canada.alternatives == ["sales territory country: DimSalesTerritory.SalesTerritoryCountry"]


def test_only_listed_columns_are_read(aw: Path) -> None:
    idx = build_value_index(aw, build_schema_graph(aw))
    assert "FactInternetSales.SalesOrderNumber" not in idx.values     # not an entity column
    assert not idx.match("order SO1")


def test_suggestions_and_hierarchy(aw: Path) -> None:
    g = build_schema_graph(aw)
    idx = build_value_index(aw, g)
    assert idx.suggest("north west")[0]["value"] == "Northwest"
    assert idx.suggest("atlas laptop") == []
    add_to_graph(g, idx)
    assert g.has_edge("value:DimGeography.StateProvinceName=Ontario",
                      "value:DimSalesTerritory.SalesTerritoryRegion=Canada")


# --- checker: digits inside a known name ---------------------------------------------------

def test_a_number_inside_a_data_name_is_not_a_claim() -> None:
    text = "Mountain-200 Black, 38 sold 300 in total."
    facts, used = {"s[0]": 300.0}, [{"value": 300.0, "fact_key": "s[0]"}]
    assert not check_section(text, facts, [], used).ok
    assert check_section(text, facts, [], used, names=["Mountain-200 Black, 38"]).ok
    assert not check_section("Mountain-200 Black, 38 sold 999.", facts, [], used,
                             names=["Mountain-200 Black, 38"]).ok


# --- the chat pipeline --------------------------------------------------------------------

def test_answered_with_filters_dates_and_a_template(aw: Path) -> None:
    p = Scripted(NW_10)
    a = ask(Q_NW, aw, p, FactsFakeProvider())
    assert a.status == "answered" and a.rows == [[300.0]]
    assert a.time_range["start"] == "2014-01-19" and a.data_as_of == "2014-01-28"
    assert a.answer_mode == "template" and "300" in a.text and len(p.calls) == 1
    assert any("partial month" in s for s in a.assumptions)


def test_clarify_then_answer(aw: Path) -> None:
    p = Scripted(NW_10)
    a = ask("What were sales in Canada last month?", aw, p)
    assert a.status == "clarify" and not p.calls and {o["metric"] for o in a.options} == {
        "internet_sales", "reseller_sales"}
    sql = ("SELECT ROUND(SUM(FactInternetSales.SalesAmount), 2) AS internet_sales FROM "
           "FactInternetSales JOIN DimSalesTerritory ON FactInternetSales.SalesTerritoryKey = "
           "DimSalesTerritory.SalesTerritoryKey WHERE DimSalesTerritory.SalesTerritoryRegion = "
           "'Canada' AND FactInternetSales.OrderDate BETWEEN '2014-01-01' AND '2014-01-28'")
    a = ask("What were sales in Canada last month?", aw, Scripted(sql), metric="internet_sales")
    assert a.status == "answered" and a.rows == [[50.0]]


def test_unknown_words_stop_the_run_before_any_model_call(aw: Path) -> None:
    for q in ("Internet sales of Atlas Laptop in the North region last 10 days",
              "Internet sales in 2013; DROP TABLE DimProduct"):
        p = Scripted("SELECT 1")
        a = ask(q, aw, p)
        assert a.status == "not_understood" and not p.calls
    a = ask("Internet sales in the North West last month", aw, Scripted("SELECT 1"))
    assert any(o["label"].startswith("Northwest") for o in a.options)


def test_fan_trap_is_refused(aw: Path) -> None:
    p = Scripted("SELECT 1")
    a = ask("Internet sales by reseller in 2013", aw, p)
    assert a.status == "refused" and not p.calls


def test_dropped_filter_is_repaired_once_then_refused(aw: Path) -> None:
    dropped = NW_10.split(" WHERE ")[0]
    p = Scripted(dropped)
    a = ask(Q_NW, aw, p)
    assert a.status == "refused" and "does not apply" in a.text and len(p.calls) == 2
    p = Scripted(dropped, NW_10)
    a = ask(Q_NW, aw, p)
    assert a.status == "answered" and a.sql_repaired and a.rows == [[300.0]]


def test_sqlite_error_gets_one_repair_turn(aw: Path) -> None:
    bad = NW_10.replace("ROUND(SUM(FactInternetSales.SalesAmount), 2) AS internet_sales",
                        "internet_sales")
    p = Scripted(bad, NW_10)
    a = ask(Q_NW, aw, p)
    assert a.status == "answered" and a.sql_repaired and len(p.calls) == 2
    assert any("no such column" in r for r in a.repair_reasons)
    p = Scripted(bad)
    assert ask(Q_NW, aw, p).status == "refused"


def test_multi_row_answer_is_model_written_and_checked(aw: Path) -> None:
    sql = ("SELECT DimSalesTerritory.SalesTerritoryRegion, ROUND(SUM(FactInternetSales.SalesAmount), "
           "2) AS internet_sales FROM FactInternetSales JOIN DimSalesTerritory ON "
           "FactInternetSales.SalesTerritoryKey = DimSalesTerritory.SalesTerritoryKey WHERE "
           "FactInternetSales.OrderDate BETWEEN '2013-01-01' AND '2014-12-31' GROUP BY 1")
    a = ask("Internet sales by sales territory region in 2013 and 2014", aw, Scripted(sql),
            FactsFakeProvider())
    assert a.status == "answered" and a.answer_mode == "model" and a.check["ok"]


def test_template_answer() -> None:
    assert template_answer("Internet sales (USD)", ["r", "s"], ["Northwest", 1234.5]) == \
        "Northwest: Internet sales (USD) 1,234.5."


def test_audit_log_is_hash_chained(aw: Path, tmp_path: Path) -> None:
    import hashlib
    log = tmp_path / "audit.jsonl"
    ask(Q_NW, aw, Scripted(NW_10), audit=log)
    ask("sales last month", aw, Scripted(NW_10), audit=log)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["prev"] == "0" * 64
    assert json.loads(lines[1])["prev"] == hashlib.sha256(lines[0].encode()).hexdigest()
    assert [json.loads(x)["status"] for x in lines] == ["answered", "clarify"]
