# The guide: how this system is built, and why

This guide explains every design decision in agentic-analytics in plain English. It is
written for someone new to the field: a student, a junior engineer, or a manager who wants
to know why an AI system that reports numbers needs so much more than a prompt.

It is also a blueprint. Each chapter says what an enterprise would use in place of the small
pieces here (SQLite, a local file), so you can rebuild the same design at work.

## What the system does, in two sentences

It writes **scheduled business reports** (a monthly sales review) and answers **ad-hoc
questions** ("What were internet sales of Mountain-200 Black, 38 in the Northwest in 2013?")
over a sales warehouse. An AI model writes the words; plain code makes sure every number
came from the data.

## The one rule behind every decision

> **No number reaches a person unless code has traced it to the data.**

Large language models (LLMs) write fluent sentences. They also invent numbers that look
right. In a report that goes to a board or a regulator, one invented number is enough to lose
trust in all of them. So the model is never the thing that is trusted. Code is.

## How to read it

Read in order the first time. Each chapter is 5 to 10 minutes.

| # | Chapter | The question it answers |
|---|---|---|
| 1 | [The problem and the big picture](01-the-problem.md) | Why not just ask a chatbot for the report? |
| 2 | [The data](02-data.md) | What is a warehouse, and how should demo data be loaded? |
| 3 | [Governed metrics and specs as code](03-metrics-and-specs.md) | Who decides what "revenue" means? |
| 4 | [Scheduled reports](04-scheduled-reports.md) | How does a report go from SQL to a published file? |
| 5 | [The number checker](05-the-number-checker.md) | How does code catch an invented number? |
| 6 | [The chatbot](06-the-chatbot.md) | How does a question become safe, correct SQL? |
| 7 | [Models and cost](07-models-and-cost.md) | Which model, where it runs, and what it costs. |
| 8 | [Testing and evaluation](08-testing-and-evaluation.md) | How do we know it works, and keeps working? |
| 9 | [Security](09-security.md) | What can go wrong on purpose, and what stops it? |
| 10 | [Deployment](10-deployment.md) | A public demo and a private enterprise system. |
| 11 | [Rebuild it in your enterprise](11-rebuild-in-your-enterprise.md) | What to swap, and in what order. |
| 12 | [RAG: the architecture decisions](12-rag-decisions.md) | Keyword or vector, rerank or not, routing, graphs, vector stores, long context, ingestion, cost. |
| 13 | [RAG in this build](13-rag-in-this-build.md) | Which of those choices this project makes, and what the measurement showed. |
| - | [Glossary](glossary.md) | Every term, one line each. |

## How each decision is written

Every important choice uses the same six parts, so you can compare them:

* **The simple way**: what most people build first.
* **What goes wrong**: the failure, with an example.
* **What we do**: the decision.
* **In an enterprise**: the production version of the same idea.
* **Where**: the file that does it.
* **Proof**: the test or measurement that shows it works.

## What is built, and what is not

This is a reference build by one engineer, on public sample data. It is not a product with
users. The guide says "built" only when the code and a test exist, and "designed" when they do
not. The [README](../../README.md) status table is the short version.

## Where the deeper records are

* `docs/adr/`: Architecture Decision Records, one per major choice, with the evidence.
* `docs/architecture.md`: the module map and where each property is enforced.
* `docs/data/README.md`: the data sample, table by table.
