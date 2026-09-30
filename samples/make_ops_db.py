"""Build ``samples/ops.sqlite``: SYNTHETIC IT-ops incidents for six months (Apr-Sep 2026).

Seeded and deterministic. One table of incidents with a severity (SEV1 worst .. SEV4),
the affected service, and the minutes from open to restore.

    python samples/make_ops_db.py [path]
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20260930
DEFAULT_PATH = Path(__file__).with_name("ops.sqlite")
START = datetime(2026, 4, 1)
END = datetime(2026, 9, 29)          # last full day of data; the report's "week" ends here
SERVICES = {                         # service -> incident weight
    "payments-api": 6, "checkout-web": 5, "auth-service": 4, "search": 4,
    "inventory-sync": 3, "notifications": 3, "reporting-etl": 2, "cdn-edge": 2,
}
SEVERITIES = {"SEV1": (1, 180), "SEV2": (3, 95), "SEV3": (8, 45), "SEV4": (12, 20)}
# severity -> (weight, typical minutes to restore)

SCHEMA = """
CREATE TABLE incidents (
    incident_id        INTEGER PRIMARY KEY,
    opened_at          TEXT NOT NULL,   -- YYYY-MM-DD HH:MM
    severity           TEXT NOT NULL,   -- SEV1 (worst) .. SEV4
    service            TEXT NOT NULL,
    minutes_to_restore INTEGER NOT NULL
)
"""


def build(path: str | Path = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(SEED)
    services, s_w = list(SERVICES), list(SERVICES.values())
    sevs, v_w = list(SEVERITIES), [w for w, _ in SEVERITIES.values()]
    rows = []
    iid = 1
    day = START
    while day <= END:
        for _ in range(rng.randint(1, 5)):
            sev = rng.choices(sevs, v_w)[0]
            typical = SEVERITIES[sev][1]
            opened = day + timedelta(minutes=rng.randint(0, 24 * 60 - 1))
            rows.append((iid, opened.strftime("%Y-%m-%d %H:%M"), sev,
                         rng.choices(services, s_w)[0],
                         max(5, round(rng.gauss(typical, typical * 0.35)))))
            iid += 1
        day += timedelta(days=1)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(SCHEMA)
        conn.executemany("INSERT INTO incidents VALUES (?, ?, ?, ?, ?)", rows)
    conn.close()
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    print(f"wrote {os.path.relpath(out)} (SYNTHETIC IT-ops data)")
