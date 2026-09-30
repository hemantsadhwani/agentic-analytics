# ADR-0003: A human approves every publish, and every decision leaves an audit row

**Status:** accepted · 2026-09-30

## Context
The checker proves numbers came from a query. It cannot tell whether the prose is fair,
whether the right query was used, or whether this week's report should go out at all.
That is a human decision, and in a regulated or board-facing setting it must be recorded.

## Decision
* The graph stops at a LangGraph `interrupt()` in the `gate` node, after checking and
  repair, before rendering. The CLI prints `PAUSED thread=<t> (publish?)`, a preview, and
  any unsupported numbers, then exits. State is checkpointed to SQLite with
  `durability="sync"`, so the approval can come from another process or another day
  (`--resume --thread <t> --decision approve|reject`).
* Approve renders and writes `reports/<spec>/<YYYY-MM-DD>.md`. Reject writes nothing.
* Both write **one** row to an `audit` table in the same SQLite file (`audit.py`): thread,
  spec, spec version (sha256 of the YAML bytes), provider, model, estimated USD, LLM calls,
  sections, numbers checked, unverified count, decision, decided_by (`$REPORT_APPROVER`),
  published path, timestamp. `thread` is the primary key and the insert is
  `INSERT OR IGNORE`, so a node replayed after a crash cannot write a second row.
* The published report's footer repeats spec version, provider/model, cost and decision.
* `--no-gate` exists for CI and evals. It auto-publishes, marks anything unsupported as
  `[unverified]`, and records `decision = "auto (--no-gate)"`. It is never used in the
  Kubernetes CronJob (`deploy/k8s/README.md` explains the trade-off).

## Consequences
* `REPORT_APPROVER` is self-declared. There is no authentication or RBAC on who may
  approve; in Kubernetes the effective control is who holds `pods/exec`.
* One report per spec per day: a second approved run on the same day overwrites the file
  (the audit keeps both rows, with different threads).
* Tests: `tests/test_graph.py` (pause, approve → file + one row, reject → no file + one
  row, idempotent insert), `tests/test_cli.py` (crash → `--resume` completes without
  re-drafting finished sections).
