# agentic-reporting

**Agents draft a report section by section from read-only SQL facts. A deterministic checker
rejects any number the model did not get from a query. A human approves before it publishes.
An audit row records it. Golden reports gate it in CI. The same pipeline runs against a local
model with no network.**

Built with LangGraph (checkpointed graph, `interrupt()` human gate), Pydantic structured
output, SQLite, and one provider interface for Claude (Anthropic API or AWS Bedrock), a local
Ollama model, or an offline fake. All sample data is **synthetic** (`samples/README.md`).

## The 60-second version

A monthly business review is numbers wrapped in sentences. LLMs write good sentences and
unreliable numbers. This repo splits the two jobs:

1. A **spec** (`specs/sales_mbr.yaml`) lists sections. Each section is one read-only `SELECT`
   and a drafting instruction. The spec is code: reviewed in git, versioned by its sha256.
2. For each section the model sees **only the rows its query returned** and must cite the
   fact key (`revenue[0]`) of every number it writes.
3. A **checker with no LLM in it** (`check.py`, regex plus arithmetic) matches every number
   in the text against that section's facts, within 0.5%. A miss triggers one repair call.
   Anything still unsupported is shown to the reviewer and, if published, replaced with
   `[unverified]`.
4. The run **pauses** for a human. Approve writes `reports/<spec>/<date>.md`; reject writes
   nothing. Both write one **audit row**: who, when, spec version, model, cost, how many
   numbers were checked.

## Enterprise properties

| Property | What it means here | Backed by |
|---|---|---|
| Template as code | Reports are YAML specs in git; unknown keys are errors | `specs/`, `agentic_reporting/spec.py`, `tests/test_spec.py` |
| SQL contract per section | One `SELECT` per section; refused at load if it could write; DB opened `mode=ro` + `query_only`; 200-row cap | `spec.py`, `data.py`, `tests/test_spec.py`, `tests/test_data.py` |
| No free-text numbers | Every number must match a query cell (0.5%); one repair; else `[unverified]` | `check.py`, `render.py`, `tests/test_check.py`, `tests/test_graph.py` |
| Human publish gate | LangGraph `interrupt()`; resume from any process, any day | `graph.py`, `tests/test_graph.py`, `tests/test_cli.py` |
| Audit row | One row per approve/reject/auto-publish, idempotent on replay | `audit.py`, `tests/test_graph.py` |
| Version hash | sha256 of the spec bytes, in the report footer and the audit row | `spec.py`, `tests/test_spec.py` |
| Golden-report gate | Both specs end to end in CI; a fabricating model must be caught | `eval/report_eval.py`, `.github/workflows/ci.yml` |
| Costed | `--max-usd` / `--max-llm-calls` checked before each call; cost in footer and audit | `budget.py`, `tests/test_graph.py::test_budget_stops_drafting_and_says_so` |
| Provider-agnostic | `LLM_PROVIDER=fake\|anthropic\|bedrock\|ollama`, one interface | `providers/`, `tests/test_router.py` |
| Private mode | Ollama run with every socket connect patched to fail: zero attempts | `tests/test_private_mode.py`, `docker-compose.yml` |
| Crash-safe | One section per checkpointed step; `--resume` continues without re-drafting | `tests/test_cli.py::test_crash_then_resume_completes_without_redrafting` |

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

## CLI

```
python -m agentic_reporting.run --spec specs/sales_mbr.yaml --db samples/sales.sqlite \
    [--out reports/] [--checkpoint-db .agentic_reporting/checkpoints.sqlite] [--thread T] \
    [--provider fake|anthropic|bedrock|ollama] [--max-usd X] [--max-llm-calls N] [--no-gate]
python -m agentic_reporting.run --resume --thread T --decision approve|reject
python -m agentic_reporting.run --resume --thread T        # continue a crashed run
python -m agentic_reporting.run --list
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
  manifests; this repo has not been applied to a live cluster.
* `.github/workflows/ci.yml`: pytest on 3.12 and 3.13, the golden-report eval, ruff, docker
  build + smoke run, kustomize render.

## What this proves, and what it does not

**Proves:** numbers in a published report trace to a read-only query cell, or are visibly
marked; nothing publishes without a recorded decision; the pipeline is resumable after a
crash, costed, provider-agnostic, and runs with no network beyond a local model; CI fails if
a golden report loses a section or a fabricated number reaches the page.

**Does not (yet):**
* no charts or tables in the report body, prose only;
* no scheduler beyond the Kubernetes CronJob shape, and no notification when a run pauses;
* no cross-report or cross-section dependencies (a section sees only its own query);
* no authentication or RBAC: `REPORT_APPROVER` is self-declared;
* no Postgres checkpointer: state is one SQLite file;
* no schema graph or text-to-SQL: a human writes each section's SQL;
* the checker cannot catch a wrong sentence built from true numbers, or an invented number
  that happens to fall within 0.5% of a fact in the same section. That is the reviewer's job.

## Layout and docs

`agentic_reporting/` (package) · `specs/` · `samples/` · `eval/` · `tests/` · `deploy/k8s/` ·
[`docs/architecture.md`](docs/architecture.md) · ADRs:
[0001 report from facts](docs/adr/0001-report-from-facts.md),
[0002 deterministic number checker](docs/adr/0002-deterministic-number-checker.md),
[0003 human publish gate and audit](docs/adr/0003-human-publish-gate-and-audit.md),
[0004 provider-agnostic and private mode](docs/adr/0004-provider-agnostic-and-private-mode.md).

MIT licence. © 2026 Hemant Sadhwani.
