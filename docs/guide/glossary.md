# Glossary

One line each. Chapter numbers show where the term is explained in context.

| Term | Meaning |
|---|---|
| Agent | Software where a model decides the next step, often by calling tools. (1) |
| Agentic workflow | Fixed steps, some done by a model. Less freedom than an agent. (1) |
| Anchor (time anchor) | The last date in the data. Relative dates count back from it. (6) |
| Attribution warning | A true number whose row or column is not named in its sentence. Shown to the reviewer. (5) |
| Audit log | A record of every decision: who, what, when, which model, what cost. (4) |
| Benchmark | Running real models on a golden set and measuring accuracy, cost and speed. (7, 8) |
| Blast radius | How much breaks if one key or system is compromised. (9) |
| Bounded autonomy | The model acts only inside limits enforced by code. (1) |
| Checkpoint | Saved state after each step, so a crashed run can resume. (4) |
| Citation (fact key) | The model's claim of where a number came from, like `revenue[0]`. (4, 5) |
| Clarifying question | The bot asks the user instead of guessing a meaning. (6) |
| Converse API | Amazon Bedrock's single way to call any of its models. (7) |
| CronJob | A Kubernetes job that runs on a timetable. (10) |
| Data lineage | Knowing exactly where each number came from. (2) |
| Data minimisation | Not loading data you do not need, especially personal data. (2) |
| Defence in depth | Several independent security layers. (9) |
| Deterministic | Same input, same output, every time. (5) |
| Dimension table | Describes things in an event: product, customer, territory. (2) |
| DPDP Act | India's Digital Personal Data Protection Act, 2023. (2, 7) |
| Escalation tier | A stronger, pricier model used only when the default fails. (7) |
| Fact (in a report) | One number from a query result, keyed by column and row. (4) |
| Fact table | One row per business event, such as a sales line. (2) |
| Fail closed | When unsure, refuse. An unknown price stops the call. (7) |
| Fan trap | A join that repeats rows, so sums come out too big. (6) |
| Foreign key | A column that points to a row in another table. (2) |
| Golden set | Inputs with known correct results, used as a test. (8) |
| Governed metric | A business number defined once, with an owner, used everywhere. (3) |
| Hash chain | Each log line stores the fingerprint of the one before; edits break the chain. (6, 9) |
| Human-in-the-loop | A person approves before an AI result takes effect. (4) |
| In-region | The model runs in the same cloud region as the data. (7) |
| Knowledge graph | Business things (products, regions) and how they relate, as a graph. (6) |
| LangGraph | A library for building model workflows as graphs, with checkpoints and pauses. (4) |
| Least privilege | Each identity gets only the access its job needs. (9) |
| Linked subset | The few tables and columns a question needs; all the SQL model sees. (6) |
| LLM | Large language model: predicts text; fluent, not a source of truth. (1) |
| LLM gateway | One place through which all model calls go. (7) |
| Mutation testing | Deliberately breaking inputs to prove a check catches them. (5) |
| Negative control | A test input built to fail, proving the gate can fail. (8) |
| OWASP LLM Top 10 | The standard list of the ten biggest security risks of LLM apps. (9) |
| Prompt injection | Text that tricks a model into ignoring its instructions. (9) |
| RAG | Retrieval-augmented generation: find relevant documents, then answer from them. (designed) |
| Read replica | A read-only copy of a database, for analytics. (2, 10) |
| Release manifest | One record of everything a release depends on: code, model, prompts, data. (7) |
| Row-level security (RLS) | The database filters rows by who is asking. (9, 10) |
| Runbook | Written steps to fix a known problem. (10) |
| Schema graph | A map of tables, columns, joins and metrics, built from metadata. (6) |
| Semantic layer | The company's shared definitions of business metrics. (3) |
| SHA-256 | A fingerprint of a file; any change gives a different fingerprint. (2, 3) |
| SLO | Service level objective: a reliability target, like "95% under 5 seconds". (10) |
| Spec (report spec) | A YAML file: per section, one SQL query and one writing instruction. (3) |
| Star schema | A warehouse layout: facts in the middle, dimensions around. (2) |
| Structured output | The model must reply in a fixed JSON shape. (4, 7) |
| Terraform | A tool that builds cloud infrastructure from code. (10) |
| Text-to-SQL | A model turns a question into a SQL query. (6) |
| Value index | A lookup from business names to exact values in the data. (6) |
| VPC | A private network inside the cloud. (10) |
