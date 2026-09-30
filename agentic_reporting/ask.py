"""CLI: ask a question of a SQLite database through the schema graph.

    python -m agentic_reporting.ask --db samples/sales.sqlite \\
        "Which region had the highest net revenue last month?"
    python -m agentic_reporting.ask --db samples/ops.sqlite --provider anthropic "..."

Prints the linked subset (tables, columns, join path, governed metric), the SQL the provider
wrote from that subset alone, and the result rows from the read-only path. Writes the schema
graph to ``.agentic_reporting/schema_graph.json`` for inspection.

The default provider is the offline ``FakeSQLProvider``, which knows only the golden
questions in ``eval/datasets/golden/schema_questions_*.json``. ``--provider
anthropic|bedrock|ollama`` sends the linked subset (never the rows) to that model.

Exit codes: 0 answered · 2 not linked (confidence below the floor; no model call)
· 3 refused (unsafe SQL, unknown metric, table outside the link) · 4 provider unavailable.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agentic_reporting.providers.base import ProviderUnavailable
from agentic_reporting.schema_graph import (
    DEFAULT_GRAPH_OUT,
    DEFAULT_METRICS,
    build_schema_graph,
    format_link,
    link,
    summary,
    write_json,
)
from agentic_reporting.sql_writer import MIN_CONFIDENCE, SQLRefused, execute, write_sql

MAX_PRINT_ROWS = 20
_BREAK_RE = re.compile(r"\s+(SELECT|FROM|JOIN|WHERE|GROUP BY|ORDER BY|LIMIT)\b")


def pretty_sql(sql: str) -> str:
    """Display only: a line break before each top-level clause. The executed SQL is unchanged."""
    out, last = [], 0
    for m in _BREAK_RE.finditer(sql):
        if sql.count("(", 0, m.start()) == sql.count(")", 0, m.start()):
            out.append(sql[last:m.start()])
            last = m.start(1)
    out.append(sql[last:])
    return "\n".join("    " + part.strip() for part in out if part.strip())


def table(columns: list[str], rows: list[list[Any]]) -> str:
    cells = [columns] + [["" if v is None else str(v) for v in r] for r in rows[:MAX_PRINT_ROWS]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
    lines = ["    " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()
             for r in cells]
    lines.insert(1, "    " + "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def get_sql_provider(name: str) -> Any:
    if name == "fake":
        from agentic_reporting.providers.fake import FakeSQLProvider
        return FakeSQLProvider()
    from agentic_reporting.providers.router import get_provider
    return get_provider(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Question → schema graph → SQL → rows (read-only).")
    ap.add_argument("question")
    ap.add_argument("--db", required=True, help="SQLite file, opened read-only")
    ap.add_argument("--provider", default="fake", choices=["fake", "anthropic", "bedrock", "ollama"],
                    help="SQL writer (default: the offline fake, golden questions only)")
    ap.add_argument("--metrics", default=str(DEFAULT_METRICS), help="governed metrics YAML")
    ap.add_argument("--graph-out", default=str(DEFAULT_GRAPH_OUT))
    args = ap.parse_args(argv)

    g = build_schema_graph(args.db, args.metrics)
    out = write_json(g, args.graph_out)
    print(f"schema graph {args.db}: {summary(g)} -> {out}")
    print(f"question: {args.question}")
    lr = link(args.question, g)
    print(format_link(lr))
    if lr.confidence < MIN_CONFIDENCE:
        print(f"NOT LINKED: confidence {lr.confidence:.2f} < {MIN_CONFIDENCE}; no SQL written, "
              "no model called")
        return 2
    try:
        provider = get_sql_provider(args.provider)
        written = write_sql(args.question, lr, provider)
        print(f"sql ({written.provider}/{written.model}, uses {', '.join(written.uses_metrics) or '-'}):")
        print(pretty_sql(written.sql))
        result = execute(args.db, written, lr)
    except SQLRefused as e:
        print(f"REFUSED: {e}")
        return 3
    except ProviderUnavailable as e:
        print(f"PROVIDER UNAVAILABLE: {e}")
        return 4
    n = len(result.rows)
    capped = ", truncated at the row cap" if result.truncated else ""
    print(f"result ({n} row{'s' if n != 1 else ''}{capped}; "
          f"read-only, tables {', '.join(lr.tables)} only):")
    print(table(result.columns, result.rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
