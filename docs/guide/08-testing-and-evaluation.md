# 8. Testing and evaluation

## Two different questions

* **Testing** asks: does the code do what it says? Same input, same output, every time.
* **Evaluation** asks: is the AI good enough? The model's output varies, so you measure it on
  a fixed set of examples and look at the rate.

An AI system needs both. Most failed AI projects had only demos.

## The layers

| Layer | What it checks | Calls a real model? | Runs |
|---|---|---|---|
| Unit tests (`tests/`, 151) | each piece: spec rules, SQL guard, checker, linker, chatbot steps | no | every push (CI) |
| Golden evals with fakes (`eval/*_eval.py --fake`) | the whole pipeline end to end, with known answers | no | every push (CI) |
| Negative controls | that each gate can actually fail | no | every push (CI) |
| Benchmarks (`--provider bedrock-converse`) | real model accuracy, cost, latency | yes | on demand; results committed |

## Decision: tests never touch the network

**What goes wrong otherwise.** Tests that call a real model are slow, cost money, need keys in
CI, and fail randomly when the model has a bad day.

**What we do.** Tests use **fake providers**: small classes that behave like a model but are
fixed. One writes correct prose from the facts. One adds a made-up number on purpose. One
returns the hand-written SQL for a golden question. CI runs with no keys at all.

## Decision: golden sets define "correct"

A **golden set** is a list of inputs with the known right result:

* `eval/datasets/golden/sales_mbr.json`, `ops_weekly.json`: each report section must exist and
  mention its required facts; nothing may be unverified.
* `chat_questions_adventureworks.json`: 22 chatbot questions. 16 must be answered, and for
  each the expected metric, tables, filters, dates and result rows are written down. Two must
  get a clarifying question, three a "not understood", one a refusal.

**Reference answers are computed independently.** Each answered question has hand-written
`reference_sql` with the dates and names typed out. The eval re-runs it every time and fails if
the data no longer gives the stored answer, so the golden set cannot silently drift.

## Decision: prove the gate can fail (negative controls)

**What goes wrong otherwise.** A test that always passes might be testing nothing. An eval that
compares two empty results says "match".

**What we do.** Every eval run includes providers built to fail:

| Control | What it does | Must happen |
|---|---|---|
| fabricating model | adds "987,654.3" to a report | caught and marked `[unverified]` |
| unsafe SQL | answers every question with `UPDATE` | refused 16 of 16, nothing executed |
| drops the filter | the right SQL with its filters and dates removed | refused 16 of 16 |
| no rows | the right SQL that returns nothing | 0 matches |

If any control passes, the eval fails. This is the testing version of "trust, but verify".

## Decision: test the checker by attacking it (mutation testing)

Chapter 5: 53 corruptions of correct numbers; every one beyond tolerance must be rejected.
46 of 46 are. When a real-model run found a new escape, it became a permanent test.

## Decision: benchmarks are code, and their results are committed

`eval/model_bench.py` (reports) and `eval/chat_eval.py` (chatbot) call real models on the
golden sets, three runs each, and write JSON to `eval/results/`. Those files are committed, so
every number in the README links to its evidence.

## Decision: a prompt change is a code change

**What goes wrong otherwise.** Someone "improves" a prompt, it reads better, and accuracy
quietly drops.

**What happened here.** We tried giving the model each metric as a ready-made SELECT item. It
sounded clearer. The benchmark said otherwise: at least 10 of 48 answers went wrong, against
none before. It was reverted the same hour. Prompts change only with a benchmark run before
and after.

## What CI runs on every push

| Job | What |
|---|---|
| test | 151 unit tests on Python 3.12 and 3.13 |
| eval | golden reports (3 providers, one fabricating) and golden schema questions |
| chat-eval | downloads AdventureWorks (pinned, checksummed), runs the 22 chatbot questions and the three controls |
| lint | ruff, pinned version |
| docker | the image builds and runs a report end to end with the fake model |
| k8s | the Kubernetes manifests render |

**In an enterprise.** The same layers, plus: evals on a sample of real (anonymised) questions,
online monitoring of answer quality and cost, and an eval gate before every model or prompt
change goes to production.

Next: [9. Security](09-security.md)
