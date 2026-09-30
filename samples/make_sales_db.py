"""Build ``samples/sales.sqlite``: SYNTHETIC orders for six months (Apr-Sep 2026).

Seeded and deterministic: the same file (same rows) on every machine. Small on purpose
(~900 orders, one table) so a reviewer can read the whole schema in one glance.

    python samples/make_sales_db.py [path]
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
from pathlib import Path

SEED = 20260930
DEFAULT_PATH = Path(__file__).with_name("sales.sqlite")
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
REGIONS = {"North": 1.25, "South": 0.9, "East": 1.05, "West": 0.8}
PRODUCTS = {                       # product -> (unit price, popularity weight)
    "Atlas Laptop": (1150.0, 3),
    "Beacon Monitor": (310.0, 5),
    "Cobalt Dock": (185.0, 4),
    "Delta Headset": (95.0, 7),
    "Ember Keyboard": (72.0, 8),
    "Flux Mouse": (38.0, 9),
    "Garnet Webcam": (120.0, 5),
    "Harbor Tablet": (640.0, 3),
}
MONTH_TREND = [1.00, 1.04, 0.97, 1.08, 1.12, 1.06]   # seasonality, applied to order count

SCHEMA = """
CREATE TABLE orders (
    order_id   INTEGER PRIMARY KEY,
    order_date TEXT NOT NULL,     -- YYYY-MM-DD
    region     TEXT NOT NULL,
    product    TEXT NOT NULL,
    units      INTEGER NOT NULL,
    revenue    REAL NOT NULL      -- units * unit price * (1 - discount), 2 dp
)
"""


def build(path: str | Path = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(SEED)
    names, weights = list(PRODUCTS), [w for _, w in PRODUCTS.values()]
    rows = []
    oid = 1
    for month, trend in zip(MONTHS, MONTH_TREND, strict=True):
        for region, size in REGIONS.items():
            for _ in range(round(38 * size * trend)):
                product = rng.choices(names, weights)[0]
                price = PRODUCTS[product][0]
                units = rng.randint(1, 6)
                discount = rng.choice([0.0, 0.0, 0.05, 0.1])
                day = rng.randint(1, 28)
                rows.append((oid, f"{month}-{day:02d}", region, product, units,
                             round(units * price * (1 - discount), 2)))
                oid += 1
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(SCHEMA)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.close()
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    print(f"wrote {os.path.relpath(out)} (SYNTHETIC sales data)")
