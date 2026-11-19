# ADR-0007: Why the assistant needs a knowledge graph, and the ontology layer on top

**Status:** problem accepted · 2026-11-17. Ontology layer: OWL, SKOS, SHACL and SPARQL **built and tested**
2026-11-19 (offline, generated from the specs). Graph database: Neo4j 5 with Cypher and a governed
text-to-Cypher path **built and tested** 2026-11-19 (local Neo4j 5.26 Community; CI service container).
The chatbot and RAG do **not** query either graph yet; Amazon Neptune is not tried.

## Problem

A sales-analytics assistant: sales directors and area teams ask in their own words. Numbers live in
a warehouse (AdventureWorks DW here); explanations live in documents. Business words do not match
the data, and nothing connects them:

1. **One thing, many names.** "MTB-200", "Mountain-200" and product key 358 are one product. A model
   that guesses the spelling gets zero rows, or silently drops the filter. The answer is wrong and
   nobody notices.
2. **Hidden hierarchies.** Product → subcategory → category; state → region → country → group.
   "Bikes in Europe" has to roll up correctly; no single table says how.
3. **Business terms are not columns.** "Revenue", "margin", "top resellers" each need one agreed
   definition, an owner and synonyms.
4. **Facts split between tables and text.** "Why did Road-150 sales fall, and what does its product
   sheet say?" needs a shared key between rows and documents.
5. **Ambiguity.** "Canada" is a region and a country; "Black" is a colour and part of a name. The
   assistant must ask, or state its assumption.
6. **Change.** New products and territories arrive every month. The vocabulary needs versions and
   checks, or answers drift.

Vectors find text that *sounds* similar; they cannot know that Road-150 belongs to Road Bikes. SQL
needs exact values and agreed definitions. A model filling that gap by guessing is the failure.

**Requirement:** one shared model of the business. An **ontology** (the kinds of things and how they
relate: Product, Territory, Metric, Document; `belongs_to`, `mentions`, `measures`) and a
**knowledge graph** (the real instances), used to resolve names, roll up hierarchies, filter
retrieval, link documents to rows, and explain every answer.

**Success measures:** names resolve to the right exact values; no filter is silently dropped;
recall@k holds on entity questions; every answer shows the concepts it used; the ontology passes
validation in CI.

## What is built (problems 1, 2, 4, 5)

| Need | Built in | Evidence |
|---|---|---|
| Schema map: tables, joins, governed metrics | `schema_graph.py` (networkx, metadata only), `specs/metrics.yaml` | ADR-0005 |
| Names → exact values; ties shown as assumptions; unknown words stop the run | `values.py`, `specs/entities.yaml` | filter-dropping queries refused 16/16 |
| Hierarchy in the graph: `value_of`, `belongs_to` (product → subcategory → category, state → region) | `values.py` `add_to_graph` | `tests/` |
| Documents linked to rows: chunks carry entity tags; a named product or subcategory filters retrieval | `documents.py`, `doc_index.py` | recall@5 14/14, RAG 19/19 |
| Governed vocabulary with synonyms and owners, reviewed in git | `specs/*.yaml` | ADR-0006 |
| The same graph in W3C standards: OWL classes, SKOS concepts, SHACL shapes, SPARQL roll-ups (below) | `ontology.py`, `ontology/*.ttl` | `tests/test_ontology.py` |
| The same graph in Neo4j: a labelled property graph loaded from the RDF, Cypher roll-ups, governed text-to-Cypher (step 5) | `graphdb.py`, `text_to_cypher.py` | `tests/test_graphdb.py`, `eval/cypher_eval.py` |

Entities come from the warehouse's master data, not from LLM extraction: that data already exists
and is exact. Category words are deliberately not used as filters (measured 16 Nov, see
`doc_index.py`).

## Decision: an ontology layer over the existing graph

Problem 6, and interoperability with other teams' data, need the graph expressed in standards.
Each step is built, tested and measured against the success measures above before any claim of it
appears outside this repo. Code: `agentic_reporting/ontology.py` (optional extra `[ontology]`:
rdflib, pyshacl); tests: `tests/test_ontology.py`; CI job `ontology`.

| # | Step | Status | What exists, and the gap |
|---|---|---|---|
| 1 | **OWL** classes and properties, generated from `specs/entities.yaml` (rdflib) | **built** | `ontology/agentic-analytics.ttl`: 23 classes (a subclass tree under `BusinessEntity`, itself a `skos:Concept`), 11 object properties (5 hierarchy levels as sub-properties of `skos:broader`, 4 relations, `mentions`, `derivesFrom`), 11 datatype properties. The spec gained `class`, `subclass_of`, `broader` and `relations` keys; the value index still reads only `column` and `label`. |
| 2 | **SKOS** for the taxonomy and the territory hierarchy | **built** | `ontology/vocabulary.ttl`: 6 concept schemes; 14 governed metrics with `prefLabel`, `altLabel` (their synonyms) and `definition`; 4 ambiguous words ("sales") as broader concepts whose `narrower` are the metrics they may mean. Instances: every value is a concept with `prefLabel`, `broader`/`narrower`, and `hiddenLabel` = the value index's match key. **Gap:** values have no synonyms in the data ("MTB-200" is not in AdventureWorks), so no entity `altLabel` is invented. |
| 3 | **SHACL** shapes in CI | **built** | `ontology/shapes.ttl`: 11 node shapes, 27 property shapes, 3 SPARQL constraints (label unique per class, no hierarchy cycle, SKOS S13). Every chunk's entity tags must resolve to a typed node. One mutation per constraint (71 in all) proves each one fires. **Gap:** "every product has exactly one subcategory" is false on AdventureWorks (209 of 504 product names, parts never sold, have none), so products get **at most one**; models, subcategories, states, regions and countries get **exactly one**. |
| 4 | **SPARQL** roll-ups tested against SQL | **built** | Product to category, models per subcategory, state to region to country to group, resellers under a territory, colour within a category, document chunks per subcategory: each equals the SQL answer, on AdventureWorks DW and on a fixture. A sales total filtered by graph-resolved names equals the SQL join. Name resolution equals the value index for every match key. |
| 5 | **Graph database** (Neo4j) with **Cypher**, and governed **text-to-Cypher** | **built** (retrieval not routed through it) | `graphdb.py` converts the RDF graph (instances + SKOS vocabulary) to a labelled property graph. Labels = the OWL class and its superclasses (`:Product:ProductTaxonomyLevel:BusinessEntity`); relationship types = the OWL object properties (`ar:inProductSubcategory` → `:IN_PRODUCT_SUBCATEGORY`); none written by hand. Loaded under a namespace label, with a uniqueness constraint on `iri` and 6 lookup indexes; idempotent (MERGE, then delete what is not in the plan). On AdventureWorks DW: **1,621 nodes** (1,485 values, 118 chunks, 14 metrics, 4 ambiguous metric words) and **3,105 relationships** in 12 types. The database equals the RDF graph node for node and edge for edge; 4 mutations (a deleted edge, a redirected edge with unchanged counts, a renamed node, a dropped label) are each caught. 10 Cypher helpers mirror the SPARQL ones and equal SQL. `text_to_cypher.py`: an LLM writes Cypher; a guard refuses writes, procedure calls, unknown labels and types, unbounded paths, hops between classes the schema does not join, and names the question did not contain; the query runs scoped to the namespace in a read transaction (5 s timeout, 200-row cap). Golden set: 24 graph questions, answers from SQL. **Gap:** the chatbot and RAG still use networkx and the value index; resellers are keyed by name (build note below). |

The instance graph is built per database and not committed (it is derived from the warehouse, like
the `.sqlite` files). On AdventureWorks DW: 12,804 triples, 1,485 value concepts, 118 document
chunks, 354 `mentions` links. `make ontology` rebuilds it.

**Not done yet:** routing the chatbot's retrieval through Neo4j; Amazon Neptune; a versioned vocabulary history (problem 6 is covered only by a spec digest in
`owl:versionInfo` and a CI drift check); answers that cite the concepts they used; OWL reasoning
(validation is SHACL; no reasoner runs).

## Not doing (now)

- LLM entity and relation extraction from text (GraphRAG): the entities exist as data. Revisit when
  a source holds relations that exist only in text (for example, a report archive).
- Loading fact rows into the graph: the warehouse keeps the numbers; the graph keeps the map.

## Consequences

- One vocabulary serves SQL filters, document filters and explanations.
- The YAML specs stay the source of truth; the RDF is generated from them, so there is one place to
  edit.
- A graph database adds infrastructure; it is justified only by traversals the in-memory graph
  cannot serve, so the networkx path stays.

## Build note, 19 Nov 2026

- `python -m agentic_reporting.ontology` writes the three `.ttl` files from the specs. `--check`
  fails when they are stale. `--db samples/adventureworks.sqlite` also builds the instance graph
  and validates it with SHACL (13,323 triples with the TBox; conforms; about 17 s).
- `tests/test_ontology.py`: 110 tests, all passing with AdventureWorks loaded. 12 skip without
  it; the whole file skips without the extra. 71 are mutation tests, one per SHACL constraint.
  7 SPARQL-against-SQL tests run on two databases each (14); 2 more check SPARQL name resolution
  against the value index, 1 checks metric words against the schema linker. 5 bad specs are refused.
- Full suite: 291 passed (181 existing + 110 new). The offline evals still pass with `--fake`:
  report, schema linking, chat and RAG.
- Found, not fixed: the schema linker asks "which sales?" for "units sold". It takes the first
  ambiguous group with any matching word, here "sold". The SKOS graph resolves it to the units
  group. The test pins this as a known gap.
- Found: the value graph's `belongs_to` edges follow every foreign key, so they also link colours
  and business types to subcategories and states. The ontology uses only the declared hierarchy.

## Build note, 19 Nov 2026: step 5, Neo4j and Cypher

- Neo4j 5.26.33 Community (Windows zip) on Eclipse Temurin JDK 21.0.13, localhost only (bolt 7687,
  http 7474), auth on. The connection comes only from `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD`;
  no password is in the repo. CI job `graphdb` runs `neo4j:5` as a service container, with a
  password made per run.
- `python -m agentic_reporting.graphdb --db samples/adventureworks.sqlite` loads, then checks the
  database against the plan (about 4 to 6 s). Running it again deletes nothing and changes nothing.
- Relationships per type on AdventureWorks: `LOCATED_IN` 701, `HAS_BUSINESS_TYPE` 701, `HAS_COLOUR`
  504, `IN_PRODUCT_SUBCATEGORY` 414, `MENTIONS` 354, `HAS_MODEL` 295, `IN_SALES_TERRITORY_REGION` 71,
  `IN_PRODUCT_CATEGORY` 37, `IN_SALES_TERRITORY_COUNTRY` 11, `NARROWER` 8,
  `IN_SALES_TERRITORY_GROUP` 7, `DERIVES_FROM` 2. `skos:broader` (540 links) is not stored twice:
  each one is a hierarchy edge.
- `tests/test_graphdb.py`: 90 tests. With Neo4j and AdventureWorks, 86 pass and 4 skip by design
  (cases that need AdventureWorks, on the fixture run). Without a server, 41 offline tests still
  run (schema, plan-vs-RDF counts, the guard: 26 refusals and 6 accepted reads, the repair loop);
  without the extra, the file skips. Each of the 10 SPARQL-vs-SQL tests has a Cypher-vs-SQL twin,
  on the fixture and on AdventureWorks. Multi-hop: chunks under a category (3 hops), resellers
  under a territory group with a business type (5 hops), and a hybrid: Cypher resolves the
  resellers and products, SQL checks who sold what.
- Defence in depth, each tested: the guard; a read transaction (the server refuses a write in it,
  `Neo.ClientError.Statement.AccessMode`); a server-side timeout (a 4-way cartesian product is
  stopped at 0.5 s); the row cap. Neo4j Community has no role-based access, so there is no
  read-only user; that would be a third layer on Enterprise or Aura.
- Text-to-Cypher on Bedrock (ap-south-1, Converse API), `eval/results/cypher_bench_*_2026-11-19.json`:
  Gemma 3 12B with DeepSeek V3.2 as the fallback answered **24/24** (dev 16/16, held-out 8/8).
  Gemma alone: 17/24 (12/16 and 5/8); the guard refused the other 7, none was answered wrong.
  DeepSeek V3.2 alone: 24/24. Cost: $0.0151 (Gemma + fallback) and $0.0358 (DeepSeek alone),
  estimated from Bedrock's token counts; all six runs of the day cost $0.076. The 16 dev questions
  were run four times while the prompt and guard were written; the 8 held-out questions were
  written afterwards and run once per model. The offline gate (`--fake`) runs hand-written Cypher
  and three negative controls (writes, misspelt names, wrong answers); each is caught.
- What the guard caught from Gemma: hops to the wrong class (`(:Reseller)-[:LOCATED_IN]->
  (:SalesTerritoryCountry)`), a name on the wrong class ("Road Bikes" as a Product), a name copied
  from the prompt's example ("Ontario"), and a `DELETE` for "delete the reseller". One repair,
  quoting the broken rule, fixed 6 of these.
- Found, not fixed: the graph keys a reseller by its **name**, and two names (Friendly Bike Shop,
  Sports Products Store) are two resellers each, in different places with different business
  types. A question that combines place and type then joins attributes of two resellers: the
  graph says Sports Products Store is a Southwest warehouse; SQL says no. The RDF graph has the
  same gap. The tests pin it; the fix is to key resellers by `ResellerKey` in the spec.
