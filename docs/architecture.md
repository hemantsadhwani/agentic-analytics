# Architecture

```
 specs/sales_mbr.yaml ──► load ──► query_all ──► draft ⟲ ──► check ──┬──► repair ──► check
 (title + sections:        │        │   (one section   (regex +      │   (once, only the
  SELECT + prompt)         │        │    per step,      arithmetic)  │    failing sections)
                           │        │    Provider)                   │
          sha256(bytes) ◄──┘        ▼                                ▼
          = spec version     SQLite, read-only                      gate  ── interrupt() ──► human
                             (validate_sql + mode=ro                 │     PAUSED thread=<t>
                              + PRAGMA query_only)                   │     --resume --decision
                             facts {"revenue[0]": 33661.95}          │
                                                        approve ◄────┴────► reject
                                                           │                   │
                                                  render (unsupported →        │
                                                  [unverified], Sources)       │
                                                           │                   │
                                                  publish reports/<spec>/<date>.md
                                                           │                   │
                                                           ▼                   ▼
                                         .agentic_reporting/checkpoints.sqlite
                                         ├─ LangGraph checkpoints (durability="sync")
                                         └─ audit table: one row per decision

 Provider (providers/): fake | anthropic | bedrock | ollama     Budget: --max-usd, --max-llm-calls
```

## Question to SQL: the schema graph (ADR-0005)

```
 SQLite metadata only                specs/metrics.yaml (governed metrics:
 (sqlite_master, table_info,          expression, grain, synonyms, owner)
  foreign_key_list; no rows)                 │
            │                                │
            ▼                                ▼
      build_schema_graph ──► networkx graph ──► .agentic_reporting/schema_graph.json
                               nodes: table · column · join · metric · term
                               edges: has_column · joins_on · derives_from · synonym_of
                                     │
 "Which region had the highest       ▼
  net revenue last month?" ──► link()  deterministic, no LLM
                               1 word overlap: names + synonyms → candidates
                               2 best metric, expanded along derives_from
                               3 shortest path over has_column/joins_on → join path
                               4 confidence (heuristic) ── < 0.5 ──► stop, no model call
                                     │
                                     ▼  LinkResult.subset(): linked tables, columns,
                                     │  join path, metric definitions. Nothing else.
                               write_sql()  ONE structured call (Provider)
                               → {sql, uses_metrics, rationale}
                                     │
                               guard: validate_sql (single SELECT, no writes)
                                      + uses_metrics ⊆ linked metrics ── fail ──► REFUSED
                                     │
                               execute(): data.run_query
                                 mode=ro + query_only + 200-row cap
                                 + SQLite authorizer: reads of linked tables only
                                     │
                                     ▼
                               rows (printed by `python -m agentic_reporting.ask`)
```

## Modules

| Module | Role |
|---|---|
| `spec.py` | YAML spec → Pydantic model; refuses anything but one read-only SELECT; `spec_version` = sha256 of the file bytes. |
| `data.py` | Runs a section's SQL read-only, caps rows at 200, flattens numeric cells into facts, collects years from date cells; optional table allow-list (SQLite authorizer). |
| `draft.py` | One structured provider call per section; the prompt carries only that section's rows and facts. |
| `check.py` | Deterministic number checker and `redact`. |
| `graph.py` | The LangGraph `StateGraph`: nodes, routing, `interrupt()` gate, publish, audit writes. |
| `render.py` | Markdown report: sections, `[unverified]` marks, Sources table, provenance footer. |
| `audit.py` | The `audit` table (stdlib `sqlite3`). |
| `run.py` | CLI: run, pause, resume, continue after a crash, list. |
| `providers/` | `Provider` protocol, Anthropic, Bedrock, Ollama, `FactsFakeProvider`, `FabricatingProvider`, scripted `FakeProvider`. |
| `budget.py` / `checkpoint.py` | Spend ceiling over the JSON state; the SQLite checkpointer. |
| `schema_graph.py` | Schema graph from SQLite metadata + `specs/metrics.yaml`; deterministic `link()`; JSON dump. |
| `sql_writer.py` | `write_sql` (one structured call over the linked subset, guarded) and `execute` (read-only, linked tables only). |
| `ask.py` | CLI: question → linked subset → SQL → rows. |

## Properties and where each one is enforced

| Property | Enforced by | Proven by |
|---|---|---|
| Template as code | `specs/*.yaml`, Pydantic `extra="forbid"` | `tests/test_spec.py` |
| SQL contract per section | `validate_sql` + `mode=ro` + `query_only` | `tests/test_spec.py`, `tests/test_data.py` |
| No free-text numbers | `check.py`, one repair, `[unverified]` in `render.py` | `tests/test_check.py`, `tests/test_graph.py`, `eval/report_eval.py` |
| Human publish gate | `gate_node` → `interrupt()` | `tests/test_graph.py`, `tests/test_cli.py` |
| Audit row | `audit.py`, written in `publish` and `reject` | `tests/test_graph.py` |
| Version hash | `spec.spec_version` in state, footer and audit | `tests/test_spec.py`, `tests/test_graph.py` |
| Golden-report gate | `eval/report_eval.py` + `eval/datasets/golden/*.json` | CI job `eval` |
| Costed | `budget.py`, provider `cost_usd`, footer + audit | `tests/test_graph.py::test_budget_stops_drafting_and_says_so` |
| Provider-agnostic | `providers/router.py` | `tests/test_router.py` |
| Private mode | `LLM_PROVIDER=ollama`, stdlib HTTP | `tests/test_private_mode.py` |
| Crash-safe | one section per step, `durability="sync"`, `--resume` | `tests/test_cli.py::test_crash_then_resume_completes_without_redrafting` |
| Schema linking without an LLM | `schema_graph.link` (lexical + shortest join path) | `tests/test_schema_graph.py`, `eval/schema_linking_eval.py` (CI job `eval`) |
| Model sees only the linked subset | `LinkResult.subset()` → `sql_writer.user_message` | `tests/test_schema_graph.py::test_prompt_carries_only_the_linked_subset` |
| Text-to-SQL cannot write or reach outside the link | `validate_sql` + `mode=ro` + `query_only` + `data.only_tables` authorizer | `tests/test_schema_graph.py`, schema eval `control/unsafe` |
