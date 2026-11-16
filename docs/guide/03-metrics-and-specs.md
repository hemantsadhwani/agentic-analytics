# 3. Governed metrics and specs as code

## Who decides what "revenue" means?

Ask three teams for "revenue last month" and you may get three numbers. One includes tax.
One subtracts returns. One uses the ship date, not the order date. None is wrong; they just
mean different things. An AI makes this worse: it picks a meaning each time it answers.

The fix is old and boring, and it works: **write the definition down once, and make every
consumer use it.**

## Decision: governed metrics in one reviewed file

**The simple way.** Let the model work out "sales" from the column names.

**What goes wrong.** Monday it sums `SalesAmount`. Tuesday it adds `TaxAmt`. Both answers look
confident. Nobody notices until two reports disagree in a meeting.

**What we do.** `specs/metrics.yaml` defines each business number once:

```yaml
- name: internet_sales
  label: Internet sales (USD)
  table: FactInternetSales
  expression: ROUND(SUM(FactInternetSales.SalesAmount), 2)
  time_column: FactInternetSales.OrderDate
  synonyms: [internet sales, online sales, web sales, ...]
  owner: Digital sales (sample owner)
  description: Sales amount of internet orders, after discounts, before tax and freight.
```

* **expression** is the exact SQL. The chatbot's model is told to copy it, never to rewrite it.
* **time_column** says which date counts (order date, not ship date).
* **synonyms** are the words people actually use. They help the chatbot find the metric.
* **owner** is who to ask, and who must approve a change.

A change to a definition is a change to this file. It goes through code review like any
other change, and git keeps the history: who changed "revenue", when, and why.

**In an enterprise.** This is called a **semantic layer** or **metrics layer**: dbt Semantic
Layer, Cube, LookML in Looker, or a metrics catalogue. Same idea, more tooling.

**Where.** `specs/metrics.yaml`, loaded by `agentic_reporting/schema_graph.py`.

## Decision: a word with two meanings is listed, and the bot asks

AdventureWorks has two sales channels: internet and resellers (bike shops). So "sales" has
two governed meanings. The metrics file says so:

```yaml
ambiguous:
  - phrases: [sales, revenue, turnover, sold, sell, sale amount]
    options: [internet_sales, reseller_sales]
    ask: This warehouse has two sales channels. Which sales do you mean?
```

If a question says only "sales", the chatbot asks this question instead of picking one. A
specific phrase like "online sales" never triggers it. Chapter 6 shows it in action.

## Decision: a report is a spec file, not a prompt

**The simple way.** Write a long prompt: "Write the monthly review. Include revenue by region,
the trend, the best product..."

**What goes wrong.** The prompt is not code. Nobody reviews it like code. Each run the model
decides what to compute, so two runs of the "same" report are different computations.

**What we do.** Each report is a YAML **spec** (`specs/sales_mbr.yaml`). It lists sections. Each
section has exactly one read-only SQL query and one short instruction for the writing:

```yaml
- id: revenue_by_region
  heading: Revenue by region
  sql: |
    WITH latest AS (SELECT MAX(substr(order_date, 1, 7)) AS m FROM orders)
    SELECT region, ROUND(SUM(revenue), 2) AS revenue, SUM(units) AS units,
           COUNT(*) AS orders
    FROM orders JOIN latest ON substr(order_date, 1, 7) = latest.m
    GROUP BY region ORDER BY revenue DESC
  prompt: >-
    For the latest month, say which region led on revenue and how the other regions
    compare. Give each region's revenue and units.
  max_words: 110
```

This is **specs as code**: new computation goes in the SQL, never in the prompt. A person
reviews the SQL once. Every run after that computes exactly the same thing.

**Validation.** The spec is checked before anything runs (`agentic_reporting/spec.py`):

* every key must be known (a typo like `promt:` is an error, not silently ignored);
* the SQL must be one `SELECT` (or `WITH ... SELECT`), with no `;`, no comments, and none of
  14 forbidden words such as `INSERT`, `DROP`, `ATTACH`, `PRAGMA`.

**Versioning.** The spec's version is the SHA-256 fingerprint of its bytes. It is printed in the
report footer and stored in the audit log. A year later you can prove which exact definition
produced a report.

**In an enterprise.** Specs live in a git repo with pull-request review and owners per spec
(a CODEOWNERS file). The scheduler runs a tagged version, not whatever is on a laptop.

**Where.** `specs/*.yaml`, `agentic_reporting/spec.py`.

**Proof.** `tests/test_spec.py` (bad keys and unsafe SQL are rejected).

## Decision: the golden set and the spec are one contract

A **golden report** (`eval/datasets/golden/*.json`) lists what each report must contain: the
sections and the facts each one must mention. Chapter 8 explains how it gates every change.
When a spec changes, its golden file changes in the same pull request, so the test and the
definition never drift apart.

Next: [4. Scheduled reports](04-scheduled-reports.md)
