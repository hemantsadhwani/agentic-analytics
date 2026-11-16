"""Write chat_questions_adventureworks.json: golden chatbot questions over AdventureWorks DW.

    python -m eval.datasets.golden.make_chat_golden      # needs samples/adventureworks.sqlite

Each answered question has hand-written ``reference_sql`` with the dates and values written
out (independent of the resolver and of any model), and ``expected`` = its result, rounded
to 4 dp. The chat eval re-runs every ``reference_sql`` and fails if it drifts from
``expected``. Other questions pin the expected behaviour: clarify, not_understood, refused.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentic_reporting.data import run_query

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "samples" / "adventureworks.sqlite"
OUT = Path(__file__).resolve().parent / "chat_questions_adventureworks.json"

FI = "FactInternetSales f"
FR = "FactResellerSales f"
P = "JOIN DimProduct p ON f.ProductKey = p.ProductKey"
S = "JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey"
C = "JOIN DimProductCategory c ON s.ProductCategoryKey = c.ProductCategoryKey"
T = "JOIN DimSalesTerritory t ON f.SalesTerritoryKey = t.SalesTerritoryKey"


def between(a: str, b: str) -> str:
    return f"f.OrderDate BETWEEN '{a}' AND '{b}'"


Y2012, Y2013 = between("2012-01-01", "2012-12-31"), between("2013-01-01", "2013-12-31")
SUM = "ROUND(SUM(f.SalesAmount), 2) AS sales"
MARGIN = "ROUND(SUM(f.SalesAmount - f.TotalProductCost), 2) AS gross_margin"

ANSWERED = [
    dict(question="What were internet sales of Mountain-200 Black, 38 in the Northwest in the last 10 days?",
         expected_metric="internet_sales",
         expected_tables=["FactInternetSales", "DimProduct", "DimSalesTerritory"],
         expected_filters={"DimProduct.EnglishProductName": ["Mountain-200 Black, 38"],
                           "DimSalesTerritory.SalesTerritoryRegion": ["Northwest"]},
         expected_time=["2014-01-19", "2014-01-28"],
         note="the last 10 days of the data had no sales of this bike there: the answer must say so",
         reference_sql=f"SELECT {SUM} FROM {FI} {P} {T} WHERE p.EnglishProductName = "
                       f"'Mountain-200 Black, 38' AND t.SalesTerritoryRegion = 'Northwest' AND "
                       f"{between('2014-01-19', '2014-01-28')}"),
    dict(question="What were internet sales of Mountain-200 Black, 38 in the Northwest in 2013?",
         expected_metric="internet_sales",
         expected_tables=["FactInternetSales", "DimProduct", "DimSalesTerritory"],
         expected_filters={"DimProduct.EnglishProductName": ["Mountain-200 Black, 38"],
                           "DimSalesTerritory.SalesTerritoryRegion": ["Northwest"]},
         expected_time=["2013-01-01", "2013-12-31"],
         reference_sql=f"SELECT {SUM} FROM {FI} {P} {T} WHERE p.EnglishProductName = "
                       f"'Mountain-200 Black, 38' AND t.SalesTerritoryRegion = 'Northwest' AND {Y2013}"),
    dict(question="What were internet sales of Mountain-200 Black in 2013?",
         expected_metric="internet_sales", expected_tables=["FactInternetSales", "DimProduct"],
         expected_filters={"DimProduct.ModelName": ["Mountain-200"], "DimProduct.Color": ["Black"]},
         expected_time=["2013-01-01", "2013-12-31"],
         note="model + colour: three sizes, no single product row is named",
         reference_sql=f"SELECT {SUM} FROM {FI} {P} WHERE p.ModelName = 'Mountain-200' AND "
                       f"p.Color = 'Black' AND {Y2013}"),
    dict(question="Which product category had the highest reseller sales in 2013?",
         expected_metric="reseller_sales",
         expected_tables=["FactResellerSales", "DimProduct", "DimProductSubcategory",
                          "DimProductCategory"],
         expected_filters={}, expected_time=["2013-01-01", "2013-12-31"],
         reference_sql=f"SELECT c.EnglishProductCategoryName, {SUM} FROM {FR} {P} {S} {C} "
                       f"WHERE {Y2013} GROUP BY 1 ORDER BY 2 DESC LIMIT 1"),
    dict(question="Internet sales by sales territory group in Q3 2013",
         expected_metric="internet_sales", expected_tables=["FactInternetSales", "DimSalesTerritory"],
         expected_filters={}, expected_time=["2013-07-01", "2013-09-30"],
         reference_sql=f"SELECT t.SalesTerritoryGroup, {SUM} FROM {FI} {T} WHERE "
                       f"{between('2013-07-01', '2013-09-30')} GROUP BY 1"),
    dict(question="How many online orders included helmets in France and Germany in 2013?",
         expected_metric="internet_orders",
         expected_tables=["FactInternetSales", "DimProduct", "DimProductSubcategory",
                          "DimSalesTerritory"],
         expected_filters={"DimProductSubcategory.EnglishProductSubcategoryName": ["Helmets"],
                           "DimSalesTerritory.SalesTerritoryRegion": ["France", "Germany"]},
         expected_time=["2013-01-01", "2013-12-31"],
         reference_sql=f"SELECT COUNT(DISTINCT f.SalesOrderNumber) AS orders FROM {FI} {P} {S} {T} WHERE "
                       "s.EnglishProductSubcategoryName = 'Helmets' AND t.SalesTerritoryRegion "
                       f"IN ('France', 'Germany') AND {Y2013}"),
    dict(question="What was the internet gross margin of Road Bikes in 2012?",
         expected_metric="internet_gross_margin",
         expected_tables=["FactInternetSales", "DimProduct", "DimProductSubcategory"],
         expected_filters={"DimProductSubcategory.EnglishProductSubcategoryName": ["Road Bikes"]},
         expected_time=["2012-01-01", "2012-12-31"],
         reference_sql=f"SELECT {MARGIN} FROM {FI} {P} {S} WHERE "
                       f"s.EnglishProductSubcategoryName = 'Road Bikes' AND {Y2012}"),
    dict(question="Top 5 products by internet sales in 2013",
         expected_metric="internet_sales", expected_tables=["FactInternetSales", "DimProduct"],
         expected_filters={}, expected_time=["2013-01-01", "2013-12-31"],
         note="a product name can have several ProductKey rows (price history): group by name",
         reference_sql=f"SELECT p.EnglishProductName, {SUM} FROM {FI} {P} WHERE {Y2013} "
                       "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"),
    dict(question="Reseller sales by business type in 2012",
         expected_metric="reseller_sales", expected_tables=["FactResellerSales", "DimReseller"],
         expected_filters={}, expected_time=["2012-01-01", "2012-12-31"],
         reference_sql=f"SELECT r.BusinessType, {SUM} FROM {FR} JOIN DimReseller r ON "
                       f"f.ResellerKey = r.ResellerKey WHERE {Y2012} GROUP BY 1"),
    dict(question="Internet sales by month in 2013",
         expected_metric="internet_sales", expected_tables=["FactInternetSales"],
         expected_filters={}, expected_time=["2013-01-01", "2013-12-31"],
         reference_sql=f"SELECT substr(f.OrderDate, 1, 7) AS month, {SUM} FROM {FI} "
                       f"WHERE {Y2013} GROUP BY 1"),
    dict(question="Reseller units of Mountain-200 last quarter",
         expected_metric="reseller_units", expected_tables=["FactResellerSales", "DimProduct"],
         expected_filters={"DimProduct.ModelName": ["Mountain-200"]},
         expected_time=["2013-10-01", "2013-11-29"],
         note="reseller data ends 2013-11-29, so its 'last quarter' differs from internet's",
         reference_sql=f"SELECT SUM(f.OrderQuantity) AS units FROM {FR} {P} WHERE p.ModelName = "
                       f"'Mountain-200' AND {between('2013-10-01', '2013-11-29')}"),
    dict(question="Internet sales in California in 2013",
         expected_metric="internet_sales",
         expected_tables=["FactInternetSales", "DimCustomer", "DimGeography"],
         expected_filters={"DimGeography.StateProvinceName": ["California"]},
         expected_time=["2013-01-01", "2013-12-31"],
         note="the customer's state: FactInternetSales → DimCustomer → DimGeography",
         reference_sql=f"SELECT {SUM} FROM {FI} JOIN DimCustomer cu ON f.CustomerKey = "
                       "cu.CustomerKey JOIN DimGeography g ON cu.GeographyKey = g.GeographyKey "
                       f"WHERE g.StateProvinceName = 'California' AND {Y2013}"),
    dict(question="Which sales territory region had the highest reseller gross margin in 2013?",
         expected_metric="reseller_gross_margin",
         expected_tables=["FactResellerSales", "DimSalesTerritory"],
         expected_filters={}, expected_time=["2013-01-01", "2013-12-31"],
         reference_sql=f"SELECT t.SalesTerritoryRegion, {MARGIN} FROM {FR} {T} WHERE {Y2013} "
                       "GROUP BY 1 ORDER BY 2 DESC LIMIT 1"),
    dict(question="Internet sales last month",
         expected_metric="internet_sales", expected_tables=["FactInternetSales"],
         expected_filters={}, expected_time=["2014-01-01", "2014-01-28"],
         note="the latest month is partial (the data stops on 28 January): the answer says so",
         reference_sql=f"SELECT {SUM} FROM {FI} WHERE {between('2014-01-01', '2014-01-28')}"),
    dict(question="What were sales in Canada last month?", metric="internet_sales",
         expected_metric="internet_sales", expected_tables=["FactInternetSales", "DimSalesTerritory"],
         expected_filters={"DimSalesTerritory.SalesTerritoryRegion": ["Canada"]},
         expected_time=["2014-01-01", "2014-01-28"],
         note="the follow-up after the clarifying question: the user chose internet sales",
         reference_sql=f"SELECT {SUM} FROM {FI} {T} WHERE t.SalesTerritoryRegion = 'Canada' AND "
                       f"{between('2014-01-01', '2014-01-28')}"),
    dict(question="Reseller sales in North America by sales territory country in 2012",
         expected_metric="reseller_sales", expected_tables=["FactResellerSales", "DimSalesTerritory"],
         expected_filters={"DimSalesTerritory.SalesTerritoryGroup": ["North America"]},
         expected_time=["2012-01-01", "2012-12-31"],
         reference_sql=f"SELECT t.SalesTerritoryCountry, {SUM} FROM {FR} {T} WHERE "
                       f"t.SalesTerritoryGroup = 'North America' AND {Y2012} GROUP BY 1"),
]

OTHER = [
    dict(question="What were sales in Canada last month?", expected_status="clarify",
         expected_options=["internet_sales", "reseller_sales"],
         note="'sales' is two governed metrics here; ask, never pick one"),
    dict(question="How many orders did we get in 2013?", expected_status="clarify",
         expected_options=["internet_orders", "reseller_orders"]),
    dict(question="Internet sales of Atlas Laptop in the North region in the last 10 days",
         expected_status="not_understood",
         note="no such product; 'North' is not a region (Northwest, Northeast are): ask, never drop"),
    dict(question="What is the weather in Paris?", expected_status="not_understood"),
    dict(question="Internet sales by reseller in 2013", expected_status="refused",
         note="resellers are reachable from internet sales only back through a shared "
              "dimension: a fan trap, refused"),
    dict(question="Internet sales in 2013; DROP TABLE DimProduct", expected_status="not_understood",
         note="injected words match nothing, so no model is called"),
]


def main() -> None:
    qs = []
    for q in ANSWERED:
        rows = run_query(DB, q["reference_sql"]).rows
        q = {"expected_status": "answered", **q,
             "expected": [[round(v, 4) if isinstance(v, float) else v for v in r] for r in rows]}
        qs.append(q)
    qs += OTHER
    OUT.write_text(json.dumps({
        "sample": "adventureworks",
        "note": "Public Microsoft AdventureWorks DW sample (MIT), loaded by "
                "samples/load_adventureworks.py. reference_sql writes the dates and values out, "
                "so it is computed independently of the resolver and of any model; expected is "
                "its result rounded to 4 dp. Regenerate with python -m "
                "eval.datasets.golden.make_chat_golden.",
        "questions": qs}, indent=2) + "\n", encoding="utf-8")
    for q in qs:
        print(f"{q['expected_status']:<15} {q['question'][:70]:<70} {q.get('expected')}")


if __name__ == "__main__":
    main()
