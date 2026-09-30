# agentic-reporting: context for Claude

Public repo. Agents draft a report section by section from read-only SQL facts; a
deterministic checker rejects any number not returned by a query; a human approves
before publish; an audit row records it; golden reports gate CI; the same pipeline runs
on a local model with no network.

## Layout

```
agentic_reporting/     the package (spec, data, draft, check, graph, render, audit, run)
agentic_reporting/providers/   Provider protocol + fake, anthropic, bedrock, ollama
specs/                 report templates (YAML): sales_mbr, ops_weekly
samples/               SYNTHETIC data generators (seeded); *.sqlite are git-ignored
eval/                  report_eval.py + datasets/golden/*.json (the CI gate)
tests/                 pytest, offline only (fake providers, socket-patched private mode)
deploy/k8s/            CronJob (runs to the gate) + approver pod + PVC, kustomize
docs/                  architecture.md, adr/0001-0004
```

## Commands

```
source .venv/bin/activate          # pip install -e ".[all]" already done
make samples test lint eval        # all offline, $0
make demo                          # pauses at the gate; prints the thread
make resume THREAD=<t> DECISION=approve
make list                          # audit rows + open threads
make demo-private                  # needs `ollama serve`; exits 0 with a hint if absent
```

Run anything that could reach a model with `ANTHROPIC_API_KEY= LOGFIRE_TOKEN=` prefixed
unless a real call is intended. No key set = the offline `fake` provider.

## Rules

* **Claims discipline.** Every README claim is backed by a file or a test. If a claim has
  no test, say "shape" or "not yet" instead. Never call a demo a product.
* Data in `samples/` is synthetic and labelled so. Never commit real data or `.env`.
* Specs are code: new computation goes in the spec's SQL, never in a prompt.
* Keep `check.py` deterministic. No LLM in the checker, ever.
* Ruff clean (`ruff check .`), tests offline. Commit messages end with the co-author line.
