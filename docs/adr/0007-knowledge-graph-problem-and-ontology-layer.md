# ADR-0007: Why the assistant needs a knowledge graph, and the ontology layer on top

**Status:** problem accepted · 2026-11-17. Ontology layer: **planned, not built.**

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

Entities come from the warehouse's master data, not from LLM extraction: that data already exists
and is exact. Category words are deliberately not used as filters (measured 16 Nov, see
`doc_index.py`).

## Decision (planned): an ontology layer over the existing graph

Problem 6, and interoperability with other teams' data, need the graph expressed in standards:

1. **OWL** classes and properties for the entity types and relations, generated from
   `specs/entities.yaml` and the schema graph (rdflib).
2. **SKOS** for the product taxonomy and the territory hierarchy: `prefLabel`, `altLabel` (the
   synonyms the value index already uses), `broader`.
3. **SHACL** shapes in CI: every product has exactly one subcategory, labels unique per class,
   every chunk's entity tags resolve to a node.
4. **SPARQL** queries for the roll-ups the chatbot needs, with tests that they match the SQL results.
5. The same graph in a **graph database** (Neo4j or Amazon Neptune) with **Cypher**, retrieval routed
   through it, and the in-memory networkx path kept as the zero-infrastructure default.

Each step is built, tested and measured against the success measures above before any claim of it
appears outside this repo.

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
