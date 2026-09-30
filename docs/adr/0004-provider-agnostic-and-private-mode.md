# ADR-0004: One provider interface; private mode runs with no network but the local model

**Status:** accepted · 2026-09-30

## Context
Enterprises differ on where model calls may go: a public API, their cloud account, or
nowhere outside the building. The pipeline should not change shape across those choices,
and the "nowhere" option has to be demonstrable, not asserted.

## Decision
Every model call goes through one `Provider` protocol (`agentic_reporting/providers/`):
`complete(system, messages, max_tokens, schema) -> LLMResponse`, with structured output
validated against a Pydantic schema and one repair round-trip. Implementations:

| `LLM_PROVIDER` | What | Cost |
|---|---|---|
| `fake` | `FactsFakeProvider`: writes prose from the facts in the prompt. Default when no key is set (one stderr line says so). | $0 |
| `anthropic` | Claude via the Anthropic API | list-price estimate |
| `bedrock` | Claude on AWS Bedrock, standard AWS credential chain | list-price estimate |
| `ollama` | A local model via Ollama's HTTP API, stdlib `urllib` only | $0 |

**Private mode** is `LLM_PROVIDER=ollama`. `tests/test_private_mode.py` patches
`socket.socket.connect`, `connect_ex` and `socket.create_connection` to raise and record,
stubs only the Ollama HTTP call at the provider boundary, runs a report through pause and
approve, and asserts zero connection attempts. `docker-compose.yml` runs the reporter next
to an `ollama/ollama` container.

Budgets (`--max-usd`, `--max-llm-calls`) are checked before each call; once exhausted, the
remaining sections are "not drafted" and the gate shows it.

## Consequences
* Swapping providers is an env var; specs, checker, gate and audit do not change.
* A small local model follows the "no digits except facts" rule less reliably than Claude,
  so expect more repairs and `[unverified]` marks. The checker makes that visible instead
  of hiding it. This repo does not include a benchmark of model quality.
* The private-mode test proves this process opens no socket other than the stubbed Ollama
  call. It does not prove anything about the machine's other processes or the OS.
