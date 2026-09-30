"""SQLite checkpointer for the report graph.

The same SQLite file also holds the ``audit`` table (see ``audit.py``), so one file is the
whole durable state of a deployment: paused threads, crashed threads, and the decision log.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_CHECKPOINT_DB = Path(".agentic_reporting") / "checkpoints.sqlite"


def default_checkpointer(path: str | os.PathLike[str] | None = None):
    """SQLite checkpointer at ``path`` / $REPORT_CHECKPOINT_DB / .agentic_reporting/checkpoints.sqlite."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = Path(path or os.environ.get("REPORT_CHECKPOINT_DB") or DEFAULT_CHECKPOINT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))
    saver.setup()
    return saver
