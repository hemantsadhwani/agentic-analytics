# ADR-0007: Why the assistant needs a knowledge graph, and the ontology layer on top

**Status:** problem accepted · 2026-11-17. Ontology layer: OWL, SKOS, SHACL and SPARQL **built and tested**
2026-11-19 (offline, generated from the specs); graph database and Cypher **planned, not built**; the
chatbot does **not** query the RDF graph yet.

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
| 5 | **Graph database** (Neo4j or Amazon Neptune) with **Cypher**, retrieval routed through it | **planned, not built** | No graph database. The chatbot and RAG still use the networkx graph and the value index; the RDF graph is a parallel, tested representation, not yet in the answer path. |

The instance graph is built per database and not committed (it is derived from the warehouse, like
the `.sqlite` files). On AdventureWorks DW: 12,804 triples, 1,485 value concepts, 118 document
chunks, 354 `mentions` links. `make ontology` rebuilds it.

**Not done yet:** a versioned vocabulary history (problem 6 is covered only by a spec digest in
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
