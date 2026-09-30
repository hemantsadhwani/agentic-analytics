# ADR-0005: A graph of the schema for schema linking; the LLM writes only the SQL text

**Status:** accepted · 2026-09-30

## Context
Until now a human wrote every section's SQL. The next step is a question in plain English
("Which region had the highest net revenue last month?") answered with SQL. The usual
failures of text-to-SQL are not syntax. They are: the wrong table, a join the schema does
not support, and a business number computed a new way each time ("revenue" gross in one
answer, net in the next). Handing a model the whole schema makes all three more likely,
and on a real warehouse the whole schema does not fit in a prompt anyway.

## Decision
**The graph holds the map; the warehouse holds the rows.** `agentic_reporting/schema_graph.py`
builds a `networkx` graph from SQLite metadata only (`sqlite_master`, `PRAGMA table_info`,
`PRAGMA foreign_key_list`). No data row is read; `tests/test_schema_graph.py` proves it with
an authorizer that denies every non-metadata read during the build.

* Nodes: tables, columns, join keys, governed metrics, metric synonyms.
* Edges: `has_column`, `joins_on`, `derives_from`, `synonym_of`.
* Join keys come from declared foreign keys, plus one inference rule: a shared `*_id`
  column that is the primary key of one side. The ops sample leaves `services.team_id`
  undeclared on purpose, so the rule is exercised.

**Governed metrics are the source of truth.** `specs/metrics.yaml` holds each metric's SQL
expression, grain, time column, synonyms and owner, reviewed in git like a report spec. A
metric is loaded into a database's graph only if its table and columns exist there.

**Linking is deterministic, with no LLM.** `link(question, graph)`:

1. Candidates by lexical overlap: a table, column, metric name or synonym links if all of
   its word stems appear in the question (lower-cased, plural `s` stripped, stop words out).
2. The highest-scoring metric wins and is expanded along `derives_from`
   (`mom_change_pct` brings in `net_revenue`).
3. Each candidate table is joined to the metric's table by the shortest path over
   `has_column`/`joins_on` edges. The union of those paths is the join path, so a table
   the question never names (`services`, between `incidents` and `teams`) is pulled in.
4. `confidence` = 0.4·[metric found] + 0.3·[every table joined] + 0.3·[share of content
   words explained]. It is a heuristic, not a probability. Below 0.5 the CLI stops before
   any model call.

**The LLM writes only the SQL text.** `sql_writer.write_sql` makes one structured call
(`{sql, uses_metrics, rationale}`) whose prompt holds only `LinkResult.subset()`: the linked
tables and columns, the join path and the metric definitions. The answer is refused unless
it passes the report-spec guard (`validate_sql`: one `SELECT`, no `;`, no comments, no write
keyword) and names only linked metrics. Execution goes through `data.run_query`: `mode=ro`,
`PRAGMA query_only`, the 200-row cap, and a new fourth lock, a SQLite authorizer that allows
reads of the linked tables only. A query that reaches outside the link fails to prepare.

**Evidence.** `eval/schema_linking_eval.py --fake` runs five golden questions per sample
database. It reports linking accuracy (tables and metric) and execution accuracy (rows
within 0.5% of a reference SQL that hard-codes the month). Two negative controls prove the
gates can fail: an `UPDATE`-writing provider (all refused, none executed) and SQL that
returns no rows (zero matches). It runs in CI.

## Consequences
* Linking is explainable and repeatable. Every linked node carries the word that linked it.
  The same question always gives the same subset, so a linking regression is a test failure.
* A metric's definition changes in one reviewed file, not in prompts.
* The model sees a few columns, not the schema: smaller prompts, fewer wrong tables.
* Lexical linking is brittle to vocabulary. A question that uses a word nobody put in the
  schema or the synonyms links nothing, or links the wrong column. The cure today is a
  synonym in `metrics.yaml`, reviewed like code.
* One primary metric per question. "Revenue and MTTR" is not supported.
* The `--fake` eval uses hand-written SQL. It proves linking, the guards, the read-only path
  and the comparison, not a model's SQL. A real-model schema eval is not built.

## Not built
* Embeddings for candidate retrieval (the lexical rule is enough for two small schemas and
  keeps the step deterministic and offline).
* Cross-database linking or federated joins: one graph per database.
* Learned or data-driven join inference (value overlap, query-log mining). Only declared
  foreign keys and the `*_id` name rule.
* Column-level read control (the authorizer is per table), and sample values in the graph.
* Text-to-SQL inside the report pipeline: specs are still written by a human.
