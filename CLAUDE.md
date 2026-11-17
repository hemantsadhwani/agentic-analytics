# agentic-analytics (repo; Python package `agentic_reporting`): context for Claude

Public repo. Agents draft a report section by section from read-only SQL facts; a
deterministic checker rejects any number not returned by a query; a human approves
before publish; an audit row records it; golden reports gate CI; the same pipeline runs
on a local model with no network.

## Layout

```
agentic_reporting/     the package (spec, data, draft, check, graph, render (+ render_html: one self-contained page,
                       tiles/decisions/SVG charts drawn from query rows), audit, run;
                       schema_graph + sql_writer + ask = question → linked schema subset → SQL → read-only result;
                       chat = the chatbot: values (entity index, specs/entities.yaml) + timeframe (dates anchored
                       on the data) + link + SQL with filter check + checked answer + hash-chained audit;
                       no metric in the question → documents route: documents (specs/documents.yaml, injection
                       screen) + embeddings (fake | Titan V2) + doc_index (FTS5 + vectors, RRF) + rag (cited, checked))
agentic_reporting/providers/   Provider protocol + fake, anthropic, bedrock, ollama
specs/                 report templates (YAML): sales_mbr, ops_weekly, aw_monthly (AdventureWorks; kinds kpis/decisions, charts;
                       rankings are SQL labels, never model work); metrics.yaml = governed metric definitions
samples/               SYNTHETIC data generators (seeded); *.sqlite are git-ignored;
                       load_adventureworks.py = Microsoft AdventureWorks DW (MIT), pinned commit +
                       sha256 per file, no customer PII; cache in samples/.cache/ (git-ignored)
eval/                  report_eval.py + schema_linking_eval.py + chat_eval.py + rag_eval.py + datasets/golden/*.json (CI gates;
                       chat_eval --fake needs samples/adventureworks.sqlite, CI downloads it);
                       chat_eval --provider bedrock-converse --model M = real chatbot benchmark;
                       model_bench.py = real Bedrock calls, picks the model by cost per passing report
                       (NOT a CI gate; results committed in eval/results/)
tests/                 pytest, offline only (fake providers, socket-patched private mode)
deploy/k8s/            CronJob (runs to the gate) + approver pod + PVC, kustomize
demo/site/ + infra/demo/   the PUBLIC demo: static page + Terraform (CloudFront, S3, API Gateway, Lambda =
                       agentic_reporting/demo_api.py, DynamoDB daily budget). Build: python infra/demo/build_lambda.py;
                       local: python -m agentic_reporting.demo_api --serve. Kill switch: -var demo_enabled=false.
                       Live since 16 Nov 2026 at https://dz5iavwk61oau.cloudfront.net (profile agentic, ap-south-1)
docs/                  architecture.md, adr/0001-0006 (0005 = schema graph: the graph holds the map, the warehouse holds the rows;
                       0006 = the chatbot: understand in code, ask when unsure, show the work; 0007 = why a knowledge
                       graph + the ontology layer, PLANNED: OWL/SKOS, SHACL, SPARQL, graph DB); data/ = AdventureWorks
```

## Commands

```
source .venv/bin/activate          # pip install -e ".[all]" already done
make samples test lint eval        # all offline, $0
make demo                          # pauses at the gate; prints the thread
make resume THREAD=<t> DECISION=approve
make list                          # audit rows + open threads
make demo-private                  # needs `ollama serve`; exits 0 with a hint if absent
make demo-ask                      # question → schema graph → SQL → rows (fake SQL writer answers the 10 golden questions only)
make demo-chat Q="..."             # the chatbot on AdventureWorks (fake = golden questions only; PROVIDER=bedrock-converse)
```

Run anything that could reach a model with `ANTHROPIC_API_KEY= LOGFIRE_TOKEN=` prefixed
unless a real call is intended. No key set = the offline `fake` provider.

## Rules

* **Claims discipline.** Every README claim is backed by a file or a test. If a claim has
  no test, say "shape" or "not yet" instead. Never call a demo a product.
* Data in `samples/` is synthetic and labelled so. Never commit real data or `.env`.
* Specs are code: new computation goes in the spec's SQL, never in a prompt.
* Generated SQL (ask.py) runs only over the tables the schema graph linked (SQLite authorizer); the
  fake SQL writer is hand-written SQL for the golden questions, so the offline eval proves the
  pipeline, not a model's SQL accuracy. Say so.
* Keep `check.py` deterministic. No LLM in the checker, ever.
* Ruff clean (`ruff check .`), tests offline. Commit messages end with the co-author line.
