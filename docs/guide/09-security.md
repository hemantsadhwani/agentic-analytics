# 9. Security

## The mindset: layers, and assume each one can fail

No single control is perfect. So security is built in **layers** (defence in depth): several
independent controls, each enough on its own, so an attacker must beat all of them at once.

## Layer 1. The AI layer can only read

Every query, from a report spec or from the chatbot's model, passes four locks:

| Lock | What it stops | Where |
|---|---|---|
| SQL text check | anything but one `SELECT`; `;`, comments, and words like `DROP`, `ATTACH`, `PRAGMA` | `spec.validate_sql` |
| Read-only connection | the database file is opened in read-only mode, so SQLite refuses writes | `data.connect_ro` |
| Read-only setting | `PRAGMA query_only = ON` on the connection | `data.connect_ro` |
| Table allow-list | the chatbot's query may read only the tables the linker chose; anything else fails before it runs | `data.only_tables` |

**In an enterprise.** A database role with `SELECT` only, on a read replica, plus
**row-level security** (RLS): the database itself filters rows by the user's region or
business unit, so even a perfect attack returns only what that user may see. Designed
(chapter 10); not built yet.

## Layer 2. Least privilege for people and machines

**Least privilege** means each identity gets only what its job needs, for only as long as it
needs it.

* The build uses its own AWS identity (`agentic-builder`), separate from any other system in the
  account, with its own network range and every resource tagged, so its cost and blast radius
  are visible. It is deleted when the build is torn down.
* **Blast radius** is how much breaks if one key leaks. Separate identities keep it small.
* No keys are stored in the repo. Credentials come from the standard AWS chain (a profile on a
  laptop, a role on a server). CI uses no keys at all.

## Layer 3. The AI never sees what it does not need

* **Data minimisation**: no customer names, e-mails, phones, addresses or birth dates are
  loaded (chapter 2).
* **The model sees the map, not the rows**: for SQL it gets only the linked tables and columns.
  For prose it gets only that section's query result.
* **The value index reads only listed columns** (`specs/entities.yaml`).

## OWASP Top 10 for LLM Applications (2025), one by one

OWASP is the standard list of web and AI security risks. Status is honest: **built** means code
and a test exist.

| Risk | What it means | What this system does | Status |
|---|---|---|---|
| LLM01 Prompt injection | text that tricks the model into ignoring its rules | the chatbot stops on unknown words before any model call; the model's SQL is checked and run read-only; the model has no tools that act (its only "tool" is the JSON reply format) | built |
| LLM02 Sensitive information disclosure | the model leaks private data | no personal data loaded; model sees only linked columns and its own query's rows | built |
| LLM03 Supply chain | a poisoned library, model or dataset | pinned data with checksums; pinned lint version; dependency pinning and scanning | data built; dependency scanning designed |
| LLM04 Data and model poisoning | bad training or retrieval data | no fine-tuning; data from a pinned, checksummed source | built (by design choice) |
| LLM05 Improper output handling | trusting model output as code or fact | SQL guard and allow-list; every number checked; JSON validated against a schema | built |
| LLM06 Excessive agency | the model can do too much | fixed workflow; no tools; read-only; human approves publishing; budgets | built |
| LLM07 System prompt leakage | secrets in prompts | prompts hold no secrets or keys | built |
| LLM08 Vector and embedding weaknesses | attacks through the search index | retrieved text treated as data, instruction-like chunks quarantined at ingestion, numbers checked against cited chunks, one embedding model per index ([chapter 13](13-rag-in-this-build.md)); a permission filter before ranking is designed for the enterprise version (row-level security on chunks) | built, except the permission filter (designed) |
| LLM09 Misinformation | confident wrong answers | number checker; ask instead of guess; show SQL and assumptions | built |
| LLM10 Unbounded consumption | runaway cost or denial of service | per-run budgets; fail-closed pricing; one repair, never a loop; public demo caps and rate limits | run limits built; demo caps designed |

## Prompt injection, in a little more detail

**Prompt injection** is the top AI risk. Someone writes, in a question or in the data,
"ignore your instructions and ...". The model may obey, because to a model, instructions and
data are both just text.

You cannot fully prevent a model from being fooled. So the design makes being fooled harmless:

1. In the chatbot, words that match nothing in the warehouse stop the run, so most injected
   sentences never reach a model at all.
2. If text does reach the model, the model can only produce a SQL string or prose. It has no
   tools and no access.
3. That SQL string is checked by code and run read-only, on allowed tables only.
4. That prose is checked number by number.

The question is not "can the model be tricked?" but "what can a tricked model do?" Here:
very little.

## Layer 4. Everything is recorded

* Reports: one audit row per publish or reject, with model, cost and spec version.
* Chatbot: one line per question, each holding the SHA-256 fingerprint of the previous line
  (a **hash chain**). Editing or deleting an old line breaks the chain, so tampering shows.

**In an enterprise.** Logs go to a central, append-only store with retention rules, alerts on
unusual use, and access reviews.

## What is not built yet (say it first)

* Login and roles (single sign-on, an approver role).
* Row-level security per user.
* Rate limiting and a cost kill switch for the public demo.
* Dependency vulnerability scanning in CI.

Next: [10. Deployment](10-deployment.md)
