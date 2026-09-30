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

## Modules

| Module | Role |
|---|---|
| `spec.py` | YAML spec → Pydantic model; refuses anything but one read-only SELECT; `spec_version` = sha256 of the file bytes. |
| `data.py` | Runs a section's SQL read-only, caps rows at 200, flattens numeric cells into facts, collects years from date cells. |
| `draft.py` | One structured provider call per section; the prompt carries only that section's rows and facts. |
| `check.py` | Deterministic number checker and `redact`. |
| `graph.py` | The LangGraph `StateGraph`: nodes, routing, `interrupt()` gate, publish, audit writes. |
| `render.py` | Markdown report: sections, `[unverified]` marks, Sources table, provenance footer. |
| `audit.py` | The `audit` table (stdlib `sqlite3`). |
| `run.py` | CLI: run, pause, resume, continue after a crash, list. |
| `providers/` | `Provider` protocol, Anthropic, Bedrock, Ollama, `FactsFakeProvider`, `FabricatingProvider`, scripted `FakeProvider`. |
| `budget.py` / `checkpoint.py` | Spend ceiling over the JSON state; the SQLite checkpointer. |

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
