# ADR-0006: The analytics chatbot understands in code, asks when unsure, and shows its work

**Status:** accepted · 2026-11-15

## Context
ADR-0005 linked a question to the schema. That is not enough to answer
"What were internet sales of Mountain-200 Black, 38 in the Northwest in the last 10 days?"
on a real warehouse (AdventureWorks DW: two fact tables, eleven dimensions). Text-to-SQL
fails in five quiet ways, and none of them is a syntax error:

1. **A value spelled wrong.** The model writes `Region = 'North West'`; zero rows come back.
2. **A word dropped.** A name the system did not recognise is ignored, and the answer is
   for a bigger question than the one asked ("Atlas Laptop" dropped → all internet sales).
3. **The wrong dates.** "Last 10 days" counted from today, on data that ends in 2014;
   or an off-by-one range.
4. **A word with two meanings.** "Sales" here is internet sales or reseller sales: two
   governed metrics, two fact tables.
5. **A join that double-counts.** Internet sales "by reseller" can only be joined back
   through a shared dimension (geography) to the other fact table: every row repeats.

## Decision
**Everything that can be decided in code is decided in code, before the model is called.**
`agentic_reporting/chat.py` runs, in order:

* **Value index** (`values.py`): distinct values of the columns listed in
  `specs/entities.yaml` only. Longest match wins, so "Mountain-200 Black, 38" is one product,
  not a model plus a colour. A tie between columns (Canada: region and country) goes to the
  first listed and the other is shown as an assumption. The listed columns are the
  governance boundary: measures, keys and customer columns are never read.
* **Time anchor** (`timeframe.py`): time words become exact dates counted back from the
  latest date in the metric's data (`data_as_of`), by documented, tested rules. A partial
  latest month is stated in the answer.
* **Linking** (`schema_graph.py`): CamelCase names, `*Key` columns joined only through
  foreign keys, and **joins only from the many side to the one side**. A table reachable
  only the other way is refused as a fan trap.
* **Ask back, never guess.** A word listed under `ambiguous:` in `specs/metrics.yaml` returns
  a clarifying question with the governed options. Any content word that matched nothing
  stops the run, with the closest known values as options. Neither case calls a model, so a
  prompt-injection sentence that matches nothing never reaches one.

**The model writes only the SQL text, from the linked subset.** The resolved filters and dates
are in the subset, and the SQL is checked to contain every filter value and both dates as
literals (one repair, then refused). A SQLite error gets one repair turn that shows the
error, then refused. Execution is read-only, linked tables only, 200 rows.

**The answer is checked like a report section.** A one-row result is answered from a template
with no model call: cheaper, and it cannot misstate the number. A larger result is
summarised by the model in the report's `{text, numbers_used}` envelope and run through the
same deterministic checker (digits inside a name from the data, like the 38 in
"Mountain-200 Black, 38", are masked as part of the name). One repair, then the template.

**Every answer shows its work**: the SQL, the metric definition and owner, the dates, the
filters, the data's last date, the check result, tokens and cost; and is appended to a
hash-chained audit log (`.agentic_reporting/chat_audit.jsonl`).

## Evidence
* `eval/chat_eval.py` over `eval/datasets/golden/chat_questions_adventureworks.json`: 22
  questions (16 answered, 2 clarify, 3 not understood, 1 refused), reference SQL with the
  dates and values written out, re-run every time so the golden answers cannot drift.
* Offline gate in CI (`chat-eval` job): behaviour, understanding and execution must be
  perfect with the reference SQL, and three negative controls must fail: an `UPDATE` (refused
  16/16), the right SQL with its filters and dates removed (refused 16/16, after its repair),
  and SQL returning no rows (0 matches).
* Real model, Gemma 3 12B on Bedrock in ap-south-1, 3 repeats
  (`eval/results/chat_bench_gemma-3-12b_2026-11-15.json`): behaviour 66/66, understanding
  48/48, execution 48/48, model-written answers passing the checker 15/15, about $0.00024 per
  answered question. 19 of 48 first drafts used a metric name as a column; the SQLite-error
  repair fixed every one.
* Escalation tier, DeepSeek V3.2, same set (`eval/results/chat_bench_deepseek-v3.2_2026-11-15.json`):
  48/48, no repairs, about $0.0015 per answered question. Gemma stays the default: the same
  accuracy at about a sixth of the cost, because the repair turn is deterministic and cheap.
* A prompt variant (the metric as a ready "select" item) was measured and lost (at least 10
  of 48 wrong, against none); it was reverted. Prompt wording is changed only when the benchmark says so.
* Unit tests on a tiny AdventureWorks-shaped database: `tests/test_chat.py`.

## Consequences
* The linker is lexical. It understands the words of the warehouse (table, column, metric
  and value names, synonyms) and asks about everything else. That is strict by design; the
  demo page lists example questions, and new phrasings are added as synonyms in review.
* Single turn only: no follow-up context ("and in France?") yet.
* A question the golden set does not cover is measured only by the benchmark, not by CI.
* "How many orders in France and Germany" is ambiguous (one total or one per country). The
  model chose per country in earlier runs; the golden answer is the total. Recorded, not hidden.
