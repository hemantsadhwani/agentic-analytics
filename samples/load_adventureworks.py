"""Build ``samples/adventureworks.sqlite`` from Microsoft's AdventureWorks DW sample (MIT).

Why this data: multi-year sales (internet and reseller channels, Dec 2010 onward) in joined
star-schema tables: products in subcategories and categories, customers in geographies,
sales territories, promotions, a date dimension. It is a public, licensed, well-known demo
warehouse, so a question like "last 10 days' sales of a product in a territory" is realistic
without any private data. Never use client data here.

Source, pinned for reproducibility (data lineage):
  github.com/microsoft/sql-server-samples @ ``COMMIT`` /
  samples/databases/adventure-works/data-warehouse-install-script/*.csv
  Licence: MIT ("Microsoft SQL Server Sample Code", Copyright (c) Microsoft Corporation).

Each CSV is checked against its sha256 before loading, so the database is byte-for-byte the
same on every machine. Downloads are cached in ``samples/.cache/`` (git-ignored).

Curation, deliberately:
* data minimisation: customer names, e-mail, phone, address and birth date are NOT loaded.
  The chatbot cannot leak what the warehouse view does not hold;
* English columns only; product photos dropped;
* foreign keys declared, so the schema graph can find the join paths;
* dates stored as ISO ``YYYY-MM-DD`` text.

    python samples/load_adventureworks.py            # build (downloads ~45 MB once)
    python samples/load_adventureworks.py --offline  # build from the cache only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache" / "adventureworks"
DEFAULT_DB = HERE / "adventureworks.sqlite"
COMMIT = "b47eadc85277f72843647223cde53aa1b85ffcb8"
BASE = ("https://raw.githubusercontent.com/microsoft/sql-server-samples/" + COMMIT +
        "/samples/databases/adventure-works/data-warehouse-install-script/")

SHA256 = {
    "DimCurrency.csv": "9ff911e2d0447204806722e736d74e9686faad7c68b9fe896446ed83264239f6",
    "DimCustomer.csv": "548dd1ffefc1419952d777c985a6c216ce32624138ffeb2743b17b9c7964b6fc",
    "DimDate.csv": "1be7b27194b4b5dae6fb3d619d81c34c0757a748be6381e5d765c42c5db132f8",
    "DimGeography.csv": "65dc2a96a63619a187215290941c19165b0a62541cd545428e169b805fbad685",
    "DimProduct.csv": "8e06a201098caef67bf6e38a93042738329d9fba5dac0f3dedb3dffbdac5c4f5",
    "DimProductCategory.csv": "1caaa9b3d7fcc7f238b1216115a502caaad5b9ca9ca035df098db515393c069d",
    "DimProductSubcategory.csv": "4496a2e75fc0279f80acb8b68a8a4e68f88b24271973f4a457e30e1a96cc1f90",
    "DimPromotion.csv": "dc54f4f21557dea85e3cf18e23c23649d71a2b740cdbb12130251a5a361dabec",
    "DimReseller.csv": "676cb16ab4c6855da557f776ff82b0dbac19fe634b7914663c1146b53a58145a",
    "DimSalesReason.csv": "89c55c34f3ecf347f57a6000d1227e8e70c8ff151be043f213a412992189f147",
    "DimSalesTerritory.csv": "24945e074b1eb4d76a3c4e8eb750f12e13eccd20f504ed52951c85dd8e3c1c64",
    "FactInternetSales.csv": "0f87171a41f56a40d7f5f6261286e3dde83c88178fff5d55a36636e6150ed3be",
    "FactInternetSalesReason.csv": "1a4ee88da072bd0d943a934954a0c2d479fff4cfa3b331f0ed1b3f20a33e37eb",
    "FactResellerSales.csv": "d682b163017d95a7c3a7e026ecfd909a09f21b4c43750a844a5ce3dea4d1e274",
}

# Source column order per file (from the pinned instawdbdw.sql CREATE TABLE statements).
_SALES = ["ProductKey", "OrderDateKey", "DueDateKey", "ShipDateKey", "{party}", "{extra}",
          "PromotionKey", "CurrencyKey", "SalesTerritoryKey", "SalesOrderNumber",
          "SalesOrderLineNumber", "RevisionNumber", "OrderQuantity", "UnitPrice",
          "ExtendedAmount", "UnitPriceDiscountPct", "DiscountAmount", "ProductStandardCost",
          "TotalProductCost", "SalesAmount", "TaxAmt", "Freight", "CarrierTrackingNumber",
          "CustomerPONumber", "OrderDate", "DueDate", "ShipDate"]
SOURCE_COLUMNS = {
    "DimDate": ["DateKey", "FullDateAlternateKey", "DayNumberOfWeek", "EnglishDayNameOfWeek",
                "SpanishDayNameOfWeek", "FrenchDayNameOfWeek", "DayNumberOfMonth",
                "DayNumberOfYear", "WeekNumberOfYear", "EnglishMonthName", "SpanishMonthName",
                "FrenchMonthName", "MonthNumberOfYear", "CalendarQuarter", "CalendarYear",
                "CalendarSemester", "FiscalQuarter", "FiscalYear", "FiscalSemester"],
    "DimCustomer": ["CustomerKey", "GeographyKey", "CustomerAlternateKey", "Title", "FirstName",
                    "MiddleName", "LastName", "NameStyle", "BirthDate", "MaritalStatus", "Suffix",
                    "Gender", "EmailAddress", "YearlyIncome", "TotalChildren",
                    "NumberChildrenAtHome", "EnglishEducation", "SpanishEducation",
                    "FrenchEducation", "EnglishOccupation", "SpanishOccupation",
                    "FrenchOccupation", "HouseOwnerFlag", "NumberCarsOwned", "AddressLine1",
                    "AddressLine2", "Phone", "DateFirstPurchase", "CommuteDistance"],
    "DimGeography": ["GeographyKey", "City", "StateProvinceCode", "StateProvinceName",
                     "CountryRegionCode", "EnglishCountryRegionName", "SpanishCountryRegionName",
                     "FrenchCountryRegionName", "PostalCode", "SalesTerritoryKey",
                     "IpAddressLocator"],
    "DimProduct": ["ProductKey", "ProductAlternateKey", "ProductSubcategoryKey",
                   "WeightUnitMeasureCode", "SizeUnitMeasureCode", "EnglishProductName",
                   "SpanishProductName", "FrenchProductName", "StandardCost", "FinishedGoodsFlag",
                   "Color", "SafetyStockLevel", "ReorderPoint", "ListPrice", "Size", "SizeRange",
                   "Weight", "DaysToManufacture", "ProductLine", "DealerPrice", "Class", "Style",
                   "ModelName", "LargePhoto", "EnglishDescription", "FrenchDescription",
                   "ChineseDescription", "ArabicDescription", "HebrewDescription",
                   "ThaiDescription", "GermanDescription", "JapaneseDescription",
                   "TurkishDescription", "StartDate", "EndDate", "Status"],
    "DimProductCategory": ["ProductCategoryKey", "ProductCategoryAlternateKey",
                           "EnglishProductCategoryName", "SpanishProductCategoryName",
                           "FrenchProductCategoryName"],
    "DimProductSubcategory": ["ProductSubcategoryKey", "ProductSubcategoryAlternateKey",
                              "EnglishProductSubcategoryName", "SpanishProductSubcategoryName",
                              "FrenchProductSubcategoryName", "ProductCategoryKey"],
    "DimPromotion": ["PromotionKey", "PromotionAlternateKey", "EnglishPromotionName",
                     "SpanishPromotionName", "FrenchPromotionName", "DiscountPct",
                     "EnglishPromotionType", "SpanishPromotionType", "FrenchPromotionType",
                     "EnglishPromotionCategory", "SpanishPromotionCategory",
                     "FrenchPromotionCategory", "StartDate", "EndDate", "MinQty", "MaxQty"],
    "DimReseller": ["ResellerKey", "GeographyKey", "ResellerAlternateKey", "Phone",
                    "BusinessType", "ResellerName", "NumberEmployees", "OrderFrequency",
                    "OrderMonth", "FirstOrderYear", "LastOrderYear", "ProductLine",
                    "AddressLine1", "AddressLine2", "AnnualSales", "BankName", "MinPaymentType",
                    "MinPaymentAmount", "AnnualRevenue", "YearOpened"],
    "DimSalesTerritory": ["SalesTerritoryKey", "SalesTerritoryAlternateKey",
                          "SalesTerritoryRegion", "SalesTerritoryCountry",
                          "SalesTerritoryGroup", "SalesTerritoryImage"],
    "DimCurrency": ["CurrencyKey", "CurrencyAlternateKey", "CurrencyName"],
    "DimSalesReason": ["SalesReasonKey", "SalesReasonAlternateKey", "SalesReasonName",
                       "SalesReasonReasonType"],
    "FactInternetSales": [c.format(party="CustomerKey", extra="_skip")
                          for c in _SALES if c != "{extra}"],
    "FactResellerSales": [c.format(party="ResellerKey", extra="EmployeeKey") for c in _SALES],
    "FactInternetSalesReason": ["SalesOrderNumber", "SalesOrderLineNumber", "SalesReasonKey"],
}

# Target schema: (column, type) kept, in order, plus foreign keys. Load order respects FKs.
I, R, T = "INTEGER", "REAL", "TEXT"  # noqa: E741 - short type aliases keep SCHEMA readable
SCHEMA: dict[str, dict] = {
    "DimDate": {"cols": [("DateKey", I), ("FullDateAlternateKey", T), ("EnglishDayNameOfWeek", T),
                         ("DayNumberOfMonth", I), ("EnglishMonthName", T),
                         ("MonthNumberOfYear", I), ("CalendarQuarter", I), ("CalendarYear", I),
                         ("FiscalQuarter", I), ("FiscalYear", I)], "pk": "DateKey"},
    "DimSalesTerritory": {"cols": [("SalesTerritoryKey", I), ("SalesTerritoryRegion", T),
                                   ("SalesTerritoryCountry", T), ("SalesTerritoryGroup", T)],
                          "pk": "SalesTerritoryKey"},
    "DimGeography": {"cols": [("GeographyKey", I), ("City", T), ("StateProvinceName", T),
                              ("EnglishCountryRegionName", T), ("PostalCode", T),
                              ("SalesTerritoryKey", I)], "pk": "GeographyKey",
                     "fks": {"SalesTerritoryKey": "DimSalesTerritory"}},
    "DimProductCategory": {"cols": [("ProductCategoryKey", I), ("EnglishProductCategoryName", T)],
                           "pk": "ProductCategoryKey"},
    "DimProductSubcategory": {"cols": [("ProductSubcategoryKey", I),
                                       ("EnglishProductSubcategoryName", T),
                                       ("ProductCategoryKey", I)], "pk": "ProductSubcategoryKey",
                              "fks": {"ProductCategoryKey": "DimProductCategory"}},
    "DimProduct": {"cols": [("ProductKey", I), ("ProductAlternateKey", T),
                            ("ProductSubcategoryKey", I), ("EnglishProductName", T), ("Color", T),
                            ("StandardCost", R), ("ListPrice", R), ("DealerPrice", R),
                            ("Size", T), ("ProductLine", T), ("Class", T), ("Style", T),
                            ("ModelName", T), ("EnglishDescription", T), ("Status", T)],
                   "pk": "ProductKey", "fks": {"ProductSubcategoryKey": "DimProductSubcategory"}},
    "DimCustomer": {"cols": [("CustomerKey", I), ("GeographyKey", I), ("MaritalStatus", T),
                             ("Gender", T), ("YearlyIncome", R), ("TotalChildren", I),
                             ("EnglishEducation", T), ("EnglishOccupation", T),
                             ("HouseOwnerFlag", I), ("NumberCarsOwned", I),
                             ("DateFirstPurchase", T), ("CommuteDistance", T)],
                    "pk": "CustomerKey", "fks": {"GeographyKey": "DimGeography"}},
    "DimReseller": {"cols": [("ResellerKey", I), ("GeographyKey", I), ("BusinessType", T),
                             ("ResellerName", T), ("NumberEmployees", I), ("ProductLine", T),
                             ("AnnualSales", R), ("YearOpened", I)],
                    "pk": "ResellerKey", "fks": {"GeographyKey": "DimGeography"}},
    "DimPromotion": {"cols": [("PromotionKey", I), ("EnglishPromotionName", T),
                              ("DiscountPct", R), ("EnglishPromotionType", T),
                              ("EnglishPromotionCategory", T), ("StartDate", T),
                              ("EndDate", T)], "pk": "PromotionKey"},
    "DimCurrency": {"cols": [("CurrencyKey", I), ("CurrencyAlternateKey", T),
                             ("CurrencyName", T)], "pk": "CurrencyKey"},
    "DimSalesReason": {"cols": [("SalesReasonKey", I), ("SalesReasonName", T),
                                ("SalesReasonReasonType", T)], "pk": "SalesReasonKey"},
}
_FACT_COLS = [("ProductKey", I), ("OrderDateKey", I), ("{party}", I), ("PromotionKey", I),
              ("CurrencyKey", I), ("SalesTerritoryKey", I), ("SalesOrderNumber", T),
              ("SalesOrderLineNumber", I), ("OrderQuantity", I), ("UnitPrice", R),
              ("DiscountAmount", R), ("TotalProductCost", R), ("SalesAmount", R), ("TaxAmt", R),
              ("Freight", R), ("OrderDate", T), ("ShipDate", T)]
_FACT_FKS = {"ProductKey": "DimProduct", "OrderDateKey": ("DimDate", "DateKey"),
             "PromotionKey": "DimPromotion",
             "CurrencyKey": "DimCurrency", "SalesTerritoryKey": "DimSalesTerritory"}
SCHEMA["FactInternetSales"] = {
    "cols": [(c.format(party="CustomerKey"), t) for c, t in _FACT_COLS],
    "fks": {**_FACT_FKS, "CustomerKey": "DimCustomer"}}
SCHEMA["FactResellerSales"] = {
    "cols": [(c.format(party="ResellerKey"), t) for c, t in _FACT_COLS],
    "fks": {**_FACT_FKS, "ResellerKey": "DimReseller"}}
SCHEMA["FactInternetSalesReason"] = {
    "cols": [("SalesOrderNumber", T), ("SalesOrderLineNumber", I), ("SalesReasonKey", I)],
    "fks": {"SalesReasonKey": "DimSalesReason"}}

DATE_COLS = {"FullDateAlternateKey", "DateFirstPurchase", "StartDate", "EndDate", "OrderDate",
             "ShipDate"}


def fetch(name: str, offline: bool) -> Path:
    path = CACHE / name
    if not path.exists():
        if offline:
            raise SystemExit(f"{path} is not cached; run once without --offline")
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE + name, timeout=120) as r:   # noqa: S310 (pinned https)
            path.write_bytes(r.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SHA256[name]:
        path.unlink()
        raise SystemExit(f"{name}: sha256 {digest} != pinned {SHA256[name]}; refusing to load")
    return path


def _value(raw: str, typ: str, col: str) -> object:
    raw = raw.strip()
    if raw == "" or raw.upper() == "NULL":
        return None
    if col in DATE_COLS:
        return raw[:10]
    if typ == I:
        return int(float(raw))
    if typ == R:
        return float(raw)
    return raw


def build(db: Path = DEFAULT_DB, offline: bool = False) -> Path:
    csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA foreign_keys = ON")
    for table, spec in SCHEMA.items():
        cols = spec["cols"]
        ddl = [f'"{c}" {t}' + (" PRIMARY KEY" if c == spec.get("pk") else "") for c, t in cols]
        for c, ref in spec.get("fks", {}).items():   # "Table" (same column name) or (table, col)
            rt, rc = ref if isinstance(ref, tuple) else (ref, c)
            ddl.append(f'FOREIGN KEY ("{c}") REFERENCES "{rt}"("{rc}")')
        con.execute(f'CREATE TABLE "{table}" ({", ".join(ddl)})')
        src = SOURCE_COLUMNS[table]
        idx = [src.index(c) for c, _ in cols]
        rows = []
        with fetch(f"{table}.csv", offline).open(encoding="utf-8-sig", newline="") as f:
            for rec in csv.reader(f, delimiter="|", quoting=csv.QUOTE_NONE):
                if not rec:
                    continue
                if len(rec) != len(src):
                    raise SystemExit(f"{table}: {len(rec)} fields, expected {len(src)}")
                rows.append([_value(rec[i], t, c) for i, (c, t) in zip(idx, cols, strict=True)])
        con.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(cols))})', rows)
    for fact in ("FactInternetSales", "FactResellerSales"):
        con.execute(f'CREATE INDEX "ix_{fact}_date" ON "{fact}"("OrderDate")')
        con.execute(f'CREATE INDEX "ix_{fact}_product" ON "{fact}"("ProductKey")')
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise SystemExit(f"foreign key violations: {bad[:5]}")
    con.commit()
    con.close()
    tmp.replace(db)
    return db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    db = build(Path(a.db), a.offline)
    con = sqlite3.connect(db)
    for t in SCHEMA:
        print(f"{t:26s} {con.execute(f'SELECT COUNT(*) FROM \"{t}\"').fetchone()[0]:>7,} rows")
    for fact in ("FactInternetSales", "FactResellerSales"):
        lo, hi = con.execute(f'SELECT MIN(OrderDate), MAX(OrderDate) FROM "{fact}"').fetchone()
        print(f"{fact}: orders {lo} .. {hi}")
    print(f"wrote {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
