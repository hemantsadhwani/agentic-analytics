# 11. Rebuild it in your enterprise

This build uses small, free pieces so anyone can run it on a laptop. The design does not depend
on them. Here is what to swap, and the order to do it in.

## What to swap

| Here | In an enterprise | Why the design survives the swap |
|---|---|---|
| SQLite sample warehouse | Snowflake, Redshift, BigQuery, Databricks, Postgres | queries are plain SQL; the read-only and allow-list ideas map to roles and grants |
| `specs/metrics.yaml` | your semantic layer (dbt, Cube, LookML) | the linker needs names, expressions, synonyms, owners: export them |
| `specs/entities.yaml` value index | a value index refreshed with each data load | same lookup, bigger; keep the "listed columns only" rule |
| report specs in git | the same, with owners per spec and pull-request review | already code |
| LangGraph checkpoints in SQLite | LangGraph with Postgres | same graph, different saver |
| audit table / JSONL file | append-only table or ledger, shipped to the log platform | same fields |
| `REPORT_APPROVER` name | single sign-on with an approver role | the gate already pauses; add an identity check on resume |
| Bedrock Gemma in Mumbai | your approved model and region, chosen by the benchmark | the provider interface hides the vendor |
| laptop / Docker / kind | ECS or Kubernetes in a private network | the image and the CronJob already exist |
| golden sets of 2 reports and 22 questions | hundreds, grown from real (anonymised) questions | same eval code |

## The order to build it

Do these in order. Each step is useful on its own, and each makes the next one safer.

1. **Agree the metrics.** Write the 10 to 20 numbers people argue about, with owners. Nothing
   else works until "revenue" means one thing.
2. **Read-only access.** A role that can only `SELECT`, on a replica, with personal data
   removed or masked.
3. **One scheduled report**, spec as code, with the number checker and a human gate. This earns
   trust: people see every number traced.
4. **Golden set and CI.** Write down what "correct" is before changing anything else.
5. **Choose the model by benchmark**, in your region, at your prices.
6. **The chatbot**, for a small group, with the ask-don't-guess rules strict at first.
7. **Identity and row-level security** before anyone outside that group uses it.
8. **Monitoring**: cost per answer, refusals, clarifications, answers corrected by users. A
   rising "not understood" rate tells you which synonyms to add.
9. **Grow the golden set** from real questions every month.

## Questions to settle with your company first

* Where may this data be processed (which country, which cloud region)?
* Which model providers are approved, and do they keep or train on your data?
* Who owns each metric definition?
* Who may approve a report, and is that approval recorded today?
* What is the budget per month, and who is alerted when it is near?
* What happens when the bot is wrong: who is told, and how is the fix tested?

## The five habits to keep, whatever you swap

1. **Decide in code** whatever can be decided in code.
2. **Give the model the smallest job** that still needs a model.
3. **Check its output with code** before a person sees it.
4. **Ask or refuse when unsure.** Never guess with numbers.
5. **Record everything**, and keep the evidence next to the claim.

Next: [12. RAG: the architecture decisions](12-rag-decisions.md) · [Guide index](README.md) · [Glossary](glossary.md)
