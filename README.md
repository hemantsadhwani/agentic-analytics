# agentic-analytics

**An analytics assistant with two capabilities over one warehouse: scheduled reports and a
chatbot. A model writes the words; code makes sure every number came from the data. A
deterministic checker rejects any number a query did not return, the chatbot asks instead of
guessing, a human approves before a report publishes, and every decision is audited. Golden
sets gate it in CI.**

> **New to this?** Start with [the guide](docs/guide/README.md): every design decision in plain
> English, why it is made, and what an enterprise would use instead.

Built with LangGraph (checkpointed graph, `interrupt()` human gate), Pydantic structured
output, SQLite, a `networkx` schema graph, and one provider interface for any Amazon Bedrock
model through the Converse API (default: Gemma 3 12B, in-region in Mumbai, chosen by
benchmark), Claude, a local Ollama model, or an offline fake. Report samples are **synthetic** (`samples/README.md`); the
chatbot work runs on **AdventureWorks DW**, Microsoft's public sample warehouse (MIT licence,
[docs/data/](docs/data/README.md)). This is my own reference build, made to enterprise standard on
public data. It is not a client system and holds no client data.

## Where it is going: an agentic analytics assistant

Scheduled reports are the first half. The second is a chatbot that answers questions over the
same warehouse and its documents, with the same rule: no number reaches a person unchecked.

| Capability | What it means | Status |
|---|---|---|
| **Scheduled agentic reports** | A YAML spec per report: one read-only SQL query and one instruction per section. A model writes the prose, a deterministic checker rejects any number the query did not return, a human approves, an audit row records it. | built |
| **Model chosen by measurement** | `eval/model_bench.py` runs the golden reports through in-region Bedrock models and ranks them by cost per passing report. | built |
| **Chatbot: questions over tables** | Governed text-to-SQL on AdventureWorks DW: names resolved to exact values, time words to exact dates (anchored on the data), one read-only query from the linked subset only, the filters checked to be in the SQL, a checked answer that shows its SQL and assumptions, a clarifying question when a word has two meanings, and a refusal for joins that double-count. [ADR-0006](docs/adr/0006-governed-analytics-chatbot.md) | built (single turn) |
| **Chatbot: questions over documents** | RAG over the product descriptions: hybrid search (keyword BM25 + vectors, rank fusion), the product named in the question narrows the search, every passage cited, every number checked against the cited text, instruction-like text quarantined at ingestion. Measured against "whole corpus in the prompt". [Decisions](docs/guide/12-rag-decisions.md), [this build](docs/guide/13-rag-in-this-build.md) | built over product descriptions; report archive next |
| **Knowledge graph** | The schema graph (tables, joins, governed metrics) extended with business entities (products, territories, resellers and their hierarchy) and the documents that mention them. | schema graph, entities and document tags built |
| **AI security** | OWASP Top 10 for LLM Applications (2025), control by control. | in progress |
| **Enterprise AWS deployment** | Private VPC in Mumbai, Cognito roles, Postgres with row-level security, Bedrock in-region, Terraform. Deploy, record evidence, destroy. | Terraform written and validated, not yet deployed |

| Measured so far | Number | Source |
|---|---|---|
| Default model | Gemma 3 12B on Amazon Bedrock, in-region in Mumbai | `eval/results/model_bench_2026-11-15.json` |
| Golden reports passed by it | 6 of 6 (2 report types x 3 runs), every section right first time | same |
| Cost per report | about $0.0006 at Bedrock list price, ap-south-1 | same |
| Corrupted numbers the checker rejects | 46 of 46 beyond the 0.5% tolerance | `tests/test_check_mutation.py` |
| Chatbot, golden questions, Gemma 3 12B x 3 runs | right behaviour 66/66 (answer, ask back, refuse); SQL results correct 48/48 | `eval/results/chat_bench_gemma-3-12b_2026-11-15.json` |
| Cost per answered question | about $0.00024 (Gemma); DeepSeek V3.2 also 48/48 at about $0.0015 | same, and `chat_bench_deepseek-v3.2_2026-11-15.json` |
| SQL with its filter removed | refused 16 of 16 (negative control, every CI run) | `eval/chat_eval.py` |
| Documents: retrieval (top 5) vs whole corpus in the prompt, Gemma 3 12B + Titan V2 x 3 runs | 19/19 vs 18/19 correct; recall@5 14/14; 401 vs 5,967 input tokens; $0.000059 vs $0.000668 per question | `eval/results/rag_bench_gemma-3-12b_titan_2026-11-16.json` |
| Planted "ignore your instructions" chunk | quarantined at ingestion in every run | `eval/rag_eval.py` |

## The 60-second version

A monthly business review is numbers wrapped in sentences. LLMs write good sentences and
unreliable numbers. This repo splits the two jobs:

1. A **spec** (`specs/sales_mbr.yaml`) lists sections. Each section is one read-only `SELECT`
   and a drafting instruction. The spec is code: reviewed in git, versioned by its sha256.
2. For each section the model sees **only the rows its query returned** and must cite the
   fact key (`revenue[0]`) of every number it writes.
3. A **checker with no LLM in it** (`check.py`, regex plus arithmetic) matches every number
   in the text against that section's facts, within 0.5%, including `112k`, `1.1 lakh`,
   `3x` and number words. Citations are enforced: an uncited number or a citation that does
   not match its fact fails the section too. A miss triggers one repair call. Anything still
   unsupported is shown to the reviewer and, if published, replaced with `[unverified]`.
   True numbers whose column or row is not named in their sentence are flagged for the
   reviewer.
4. The run **pauses** for a human. Approve writes `reports/<spec>/<date>.md`; reject writes
   nothing. Both write one **audit row**: who, when, spec version, model, cost, how many
   numbers were checked.

## Enterprise properties

| Property | What it means here | Backed by |
|---|---|---|
| Template as code | Reports are YAML specs in git; unknown keys are errors | `specs/`, `agentic_reporting/spec.py`, `tests/test_spec.py` |
| SQL contract per section | One `SELECT` per section; refused at load if it could write; DB opened `mode=ro` + `query_only`; 200-row cap | `spec.py`, `data.py`, `tests/test_spec.py`, `tests/test_data.py` |
| No free-text numbers | Every number must match a query cell (0.5%) and be cited; one repair; else `[unverified]`. Every audit probe pinned; a mutation test rejects 46/46 non-equivalent corruptions | `check.py`, `render.py`, `tests/test_check.py`, `tests/test_check_probes.py`, `tests/test_check_mutation.py`, `tests/test_graph.py` |
| Human publish gate | LangGraph `interrupt()`; resume from any process, any day | `graph.py`, `tests/test_graph.py`, `tests/test_cli.py` |
| Audit row | One row per approve/reject/auto-publish, idempotent on replay | `audit.py`, `tests/test_graph.py` |
| Version hash | sha256 of the spec bytes, in the report footer and the audit row | `spec.py`, `tests/test_spec.py` |
| Golden-report gate | Both specs end to end in CI; a fabricating model must be caught | `eval/report_eval.py`, `.github/workflows/ci.yml` |
| Costed | `--max-usd` / `--max-llm-calls` checked before each call; cost in footer and audit | `budget.py`, `tests/test_graph.py::test_budget_stops_drafting_and_says_so` |
| Provider-agnostic | `LLM_PROVIDER=fake\|anthropic\|bedrock\|bedrock-converse\|ollama`, one interface. `bedrock-converse` reaches any Bedrock model; default Gemma 3 12B in `ap-south-1`, chosen by `eval/model_bench.py` (6/6 golden reports, about $0.0006 per report). Unpriced models are refused before any call | `providers/`, `tests/test_router.py`, `tests/test_converse_provider.py`, `eval/results/` |
| Private mode | Ollama run with every socket connect patched to fail: zero attempts | `tests/test_private_mode.py`, `docker-compose.yml` |
| Crash-safe | One section per checkpointed step; `--resume` continues without re-drafting | `tests/test_cli.py::test_crash_then_resume_completes_without_redrafting` |
| Schema graph linking | Deterministic question → tables, join path, governed metric; the model sees only that subset; SQL guarded, read-only, linked tables only | `schema_graph.py`, `sql_writer.py`, `specs/metrics.yaml`, `tests/test_schema_graph.py`, `eval/schema_linking_eval.py` |

## Quickstart (offline, $0, no key)

```
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e ".[all]"
make demo                                             # builds synthetic data, drafts, PAUSES
make resume THREAD=<thread from the output> DECISION=approve
make list                                             # the audit log
```

With no `LLM_PROVIDER` and no `ANTHROPIC_API_KEY`, the offline `fake` provider is used and
one line on stderr says so. It writes mechanical one-sentence-per-row prose from the facts in
the prompt: it proves the pipeline, not a model's writing.

## Real output

`make demo` (captured 30 Sep 2026):

```
run thread=sales_mbr-20260930T040903Z spec=specs/sales_mbr.yaml provider=fake
  · draft 1/4 summary
  · draft 2/4 revenue_by_region
  · draft 3/4 top_products
  · draft 4/4 month_on_month
  · check: 37 numbers, all traced to a query
PAUSED thread=sales_mbr-20260930T040903Z (publish?)
  | Summary: 2026-09: revenue 111,850.2, prior month revenue 124,013.2, change pct -9.8, orders 160.
  | Revenue by region: North: revenue 33,661.9, units 155, orders 50. East: revenue 29,832.2, units 143, orders 42. South: revenue 2…
  | Top products: Atlas Laptop: revenue 47,955, units 43. Harbor Tablet: revenue 15,776, units 26. Beacon Monitor: revenue 12,0…
  | Month-on-month change: 2026-04: revenue 98,843.1. 2026-05: revenue 129,585.9, change pct 31.1. 2026-06: revenue 105,965.2, change pc…
  unsupported: none
  approve: python -m agentic_reporting.run --resume --thread sales_mbr-20260930T040903Z --decision approve   (or --decision reject)
report: 4 sections, 37 numbers checked, 0 unverified
budget: $0.0000/∞ · llm calls 4/∞ · stopped: paused
```

`make resume THREAD=sales_mbr-20260930T040903Z DECISION=approve`:

```
PUBLISHED reports/sales_mbr/2026-09-30.md
report: 4 sections, 37 numbers checked, 0 unverified
budget: $0.0000/∞ · llm calls 4/∞ · stopped: published
```

The published file ends with a Sources table per section (fact key → the exact value the
query returned) and a provenance line:

```
_spec `sales_mbr` version `sha256:037f9f974466` · provider `fake/facts-fake-1` · cost $0.0000 (4 llm calls, list-price estimate) · decision `approved` by `local-user` · thread `sales_mbr-20260930T040903Z`_
```

`make list` after one approve, one reject and one `--no-gate` run:

```
audit (.agentic_reporting/checkpoints.sqlite):
  2026-09-30T04:09:30+00:00  ops_weekly-20260930T040930Z        auto (--no-gate) by none (--no-gate) spec ops_weekly@88374a73  fake/facts-fake-1  $0.0000 4 calls  4 sections  22 checked  0 unverified
  2026-09-30T04:09:27+00:00  sales_mbr-20260930T040917Z         rejected         by hemant       spec sales_mbr@037f9f97  fake/facts-fake-1  $0.0000 4 calls  4 sections  37 checked  0 unverified
  2026-09-30T04:09:14+00:00  sales_mbr-20260930T040903Z         approved         by local-user   spec sales_mbr@037f9f97  fake/facts-fake-1  $0.0000 4 calls  4 sections  37 checked  0 unverified
```

`make eval` (the CI gate). `fabricate-once` adds a made-up number to every first draft (the
repair must clear it); `fabricate-always` keeps adding it (it must be published as
`[unverified]`, never as a number):

```
  ok   ops_weekly/facts               4 sections · 22 numbers checked · 0 unverified · repaired 0 · fake/facts-fake-1 4 calls
  ok   ops_weekly/fabricate-once      4 sections · 22 numbers checked · 0 unverified · repaired 4 · fake/fabricating-fake-1 8 calls
  ok   ops_weekly/fabricate-always    4 sections · 26 numbers checked · 4 unverified · repaired 4 · fake/fabricating-fake-1 8 calls
  ok   sales_mbr/facts                4 sections · 37 numbers checked · 0 unverified · repaired 0 · fake/facts-fake-1 4 calls
  ok   sales_mbr/fabricate-once       4 sections · 37 numbers checked · 0 unverified · repaired 4 · fake/fabricating-fake-1 8 calls
  ok   sales_mbr/fabricate-always     4 sections · 41 numbers checked · 4 unverified · repaired 4 · fake/fabricating-fake-1 8 calls
REPORT-EVAL PASS (2 golden reports × 3 providers)
```

## From question to SQL: the schema graph

A second path answers a plain-English question with SQL, without showing the model the
schema or the rows. **The graph holds the map; the warehouse holds the rows** (ADR-0005):

1. `schema_graph.py` builds a `networkx` graph from SQLite **metadata only**: tables,
   columns, join keys (declared foreign keys, plus a shared `*_id` primary key), and
   **governed metrics** from [`specs/metrics.yaml`](specs/metrics.yaml) (SQL expression,
   grain, synonyms, owner). It is written to `.agentic_reporting/schema_graph.json`.
2. `link()` is **deterministic, no LLM**: word overlap with table, column and metric names
   and synonyms, then the shortest join path through the graph.
3. `write_sql()` makes **one** structured call that sees only the linked subset and returns
   `{sql, uses_metrics, rationale}`. The SQL gets the report-spec guard (one `SELECT`, no
   writes), then runs read-only with the row cap and a SQLite authorizer that allows reads of
   the **linked tables only**.

`make demo-ask` (offline fake SQL writer, captured 30 Sep 2026):

```
schema graph samples/sales.sqlite: 3 tables, 14 columns, 2 joins, 3 metrics, 16 terms; edges: 14 has_column, 4 joins_on, 4 derives_from, 16 synonym_of -> .agentic_reporting/schema_graph.json
question: Which region had the highest net revenue last month?
linked  confidence 1.00
  tables   orders
  columns  orders.revenue, orders.order_date, orders.region
  joins    - (single table)
  metric   net_revenue = ROUND(SUM(orders.revenue), 2)  [owner: Finance analytics (synthetic owner)]
sql (fake/sql-fake-1, uses net_revenue):
    WITH latest AS (SELECT MAX(substr(order_date, 1, 7)) AS m FROM orders)
    SELECT orders.region AS region, ROUND(SUM(orders.revenue), 2) AS net_revenue
    FROM orders
    JOIN latest ON substr(orders.order_date, 1, 7) = latest.m
    GROUP BY orders.region
    ORDER BY net_revenue DESC
    LIMIT 1
result (1 row; read-only, tables orders only):
    region | net_revenue
    -------+------------
    North  | 33661.95
```

A two-hop join the question never spells out (`make demo-ask DATA=samples/ops.sqlite
Q="Which team had the most incidents last month?"`) links `incidents → services → teams`,
the second join inferred from the undeclared `services.team_id`:

```
  tables   incidents, services, teams
  joins    incidents.service = services.service  (foreign key); services.team_id = teams.team_id  (name)
  metric   incident_count = COUNT(incidents.incident_id)  [owner: SRE leadership (synthetic owner)]
```

`python -m eval.schema_linking_eval --fake` (in `make eval` and CI): five golden questions
per sample DB, each with expected tables, expected metric and a reference SQL result.

```
linking accuracy 10/10 (100%) · execution accuracy 10/10 (100%) · tolerance 0.5%
SCHEMA-EVAL PASS (2 sample DBs, 10 questions, provider fake/sql-fake-1)
```

Two negative controls run with it: a provider that answers with `UPDATE` (10/10 refused,
none executed) and SQL that returns no rows (0/10 matches). The fake writer's SQL is
hand-written: this proves linking, the guards, the read-only path and the comparison, not a
model's SQL. It knows only the ten golden questions; for others pass
`--provider anthropic|bedrock|ollama`. A question that links nothing (confidence below 0.5)
stops before any model call.

## CLI

```
python -m agentic_reporting.run --spec specs/sales_mbr.yaml --db samples/sales.sqlite \
    [--out reports/] [--checkpoint-db .agentic_reporting/checkpoints.sqlite] [--thread T] \
    [--provider fake|anthropic|bedrock|ollama] [--max-usd X] [--max-llm-calls N] [--no-gate]
python -m agentic_reporting.run --resume --thread T --decision approve|reject
python -m agentic_reporting.run --resume --thread T        # continue a crashed run
python -m agentic_reporting.run --list
python -m agentic_reporting.ask --db samples/sales.sqlite "<question>" [--provider fake|anthropic|bedrock|ollama]
```

`--db` is the data source (read-only). `--checkpoint-db` holds run state and the audit table.
`--no-gate` skips the human and auto-publishes, with unsupported numbers as `[unverified]`;
it is for CI and evals, and the audit row says so. `REPORT_APPROVER` names the approver
(default `local-user`).

## Run it privately

Nothing leaves the machine except the call to your local Ollama server:

```
ollama serve &  ollama pull qwen2.5:7b-instruct
make demo-private                 # prints how to start Ollama and exits 0 if it is not running
```

Or in containers, the reporter next to an `ollama/ollama` service:

```
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b-instruct
docker compose run --rm reporter --provider ollama --spec specs/sales_mbr.yaml --db samples/sales.sqlite
docker compose run --rm reporter --resume --thread <thread> --decision approve
```

`tests/test_private_mode.py` is the evidence: it patches every socket connect to fail, stubs
only the Ollama HTTP call, runs a report through pause and approve, and asserts zero
connection attempts. A small local model follows the "no digits except facts" rule less
reliably than Claude; expect more repairs and `[unverified]` marks. No real local-model run is
shown in this README.

Claude on Bedrock: `make demo-bedrock` (AWS credentials from the standard chain, capped at
`--max-usd 0.05`). Anthropic direct: set `ANTHROPIC_API_KEY`.

## Deploy

* `Dockerfile`: `python:3.13-slim`, non-root (uid 10001), `ENTRYPOINT python -m agentic_reporting.run`.
* `deploy/k8s/`: a weekly `CronJob` that runs the report **up to the gate** and stops, an idle
  approver pod to `kubectl exec` the `--resume`, a PVC for state and reports, kustomize.
  `deploy/k8s/README.md` explains why the CronJob does not use `--no-gate`. CI renders the
  manifests. Verified on a local kind cluster on 30 Sep 2026: the job paused at the gate, an
  approve from the approver pod published the report to the PVC and wrote the audit row. Never
  claimed as a production deployment.
* `.github/workflows/ci.yml`: pytest on 3.12 and 3.13, the golden-report and schema-linking evals, ruff, docker
  build + smoke run, kustomize render.

## What this proves, and what it does not

**Proves:** numbers in a published report trace to a read-only query cell, or are visibly
marked; nothing publishes without a recorded decision; the pipeline is resumable after a
crash, costed, provider-agnostic, and runs with no network beyond a local model; CI fails if
a golden report loses a section or a fabricated number reaches the page. A plain-English
question is linked to tables, a join path and a governed metric with no LLM, and the SQL the
model writes from that subset alone is refused if it writes, names an unlinked metric, or
reads an unlinked table; CI fails if a golden question links or answers wrongly.

**Does not (yet):**
* no charts or tables in the report body, prose only;
* no scheduler beyond the Kubernetes CronJob shape, and no notification when a run pauses;
* no cross-report or cross-section dependencies (a section sees only its own query);
* no authentication or RBAC: `REPORT_APPROVER` is self-declared;
* no Postgres checkpointer: state is one SQLite file;
* report sections still use human-written SQL: text-to-SQL is a separate `ask` path, not
  wired into the report pipeline;
* schema linking is lexical: no embeddings, one primary metric per question, one database
  per graph, joins only from foreign keys and shared `*_id` keys; unknown vocabulary links
  nothing until someone adds a synonym;
* the schema eval runs hand-written fake SQL only: no real-model text-to-SQL accuracy is
  measured or claimed;
* the checker cannot prove a sentence built from true numbers is true: it flags a number
  whose column or row is not named nearby, but meaning is the reviewer's job. An invented
  number within 0.5% of a fact in the same section passes; so do `twice`, `half`, `doubled`.

## Layout and docs

`agentic_reporting/` (package) · `specs/` · `samples/` · `eval/` · `tests/` · `deploy/k8s/` ·
[`docs/architecture.md`](docs/architecture.md) · [`docs/data/`](docs/data/README.md) (the AdventureWorks warehouse) · ADRs:
[0001 report from facts](docs/adr/0001-report-from-facts.md),
[0002 deterministic number checker](docs/adr/0002-deterministic-number-checker.md),
[0003 human publish gate and audit](docs/adr/0003-human-publish-gate-and-audit.md),
[0004 provider-agnostic and private mode](docs/adr/0004-provider-agnostic-and-private-mode.md),
[0005 schema graph for schema linking](docs/adr/0005-schema-graph-for-schema-linking.md).

MIT licence. © 2026 Hemant Sadhwani.
