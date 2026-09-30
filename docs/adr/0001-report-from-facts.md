# ADR-0001: Draft reports from query facts, never from the model's memory

**Status:** accepted · 2026-09-30

## Context
A business report is a set of numbers wrapped in sentences. An LLM is good at the
sentences and unreliable at the numbers: it rounds, sums, remembers last quarter, or
invents a plausible figure. A report generator that lets the model "look up" or "work out"
numbers produces text nobody can sign off.

## Decision
A report is a **spec** (`specs/*.yaml`): a title and a list of sections. Each section is one
read-only `SELECT` plus a drafting instruction and a word limit. The spec is the template,
kept in git, reviewed like code, and versioned by the sha256 of its bytes.

For each section the pipeline runs the SQL (`data.py`), flattens every numeric cell into a
fact keyed `<column>[<row>]`, and hands the model only that section's rows and facts
(`draft.py`). The model returns prose plus the fact key of every number it used, validated
against a Pydantic schema. The model never sees other sections' data, never sees the
database, and never runs SQL.

**SQL contract per section.** Three locks keep a spec from writing:
1. `spec.validate_sql`: one statement, starts with `SELECT`/`WITH`, no comments, no
   `INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA/...` keyword anywhere (deliberately blunt);
2. the database is opened with a read-only URI (`mode=ro`), so SQLite refuses writes and
   never creates a missing file;
3. `PRAGMA query_only = ON`.

Results are capped at 200 rows; a truncated result is flagged to the model.

## Consequences
* Computation lives in SQL, where it is reproducible and reviewable. "Month-on-month
  change" is a `LAG()` in the spec, not arithmetic in a prompt.
* Section-by-section drafting makes each call small and each failure local, and lets the
  graph checkpoint after every section (`graph.py`).
* A cross-section narrative ("revenue fell because the West region shrank") can only use
  numbers its own query returns. Sections cannot depend on each other yet.
* Tests: `tests/test_spec.py`, `tests/test_data.py`.
