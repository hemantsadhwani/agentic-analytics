# 13. RAG in this build: the choices and how they will be measured

Chapter 12 gave the general decisions. This chapter applies them to agentic-analytics, with the
real numbers that drive each choice.

> **Status: designed, not built.** Everything below is the plan. It becomes "built" in the README
> only when the code, the tests and the measurements exist.

## Start from the facts

| Fact (checked 16 Nov 2026) | Why it matters |
|---|---|
| The product catalogue has 114 distinct descriptions, about 2,200 tokens in all | tiny: it fits in any prompt |
| A published report is about 500 to 700 tokens; the archive grows every month | the corpus grows; a monthly review for each month of AdventureWorks history is about 37 reports |
| Gemma 3 models are built for up to 128,000 tokens of context | everything fits in the prompt today |
| In Mumbai, Bedrock serves embedding models in-region: Titan Text Embeddings V2 ($0.024 per million tokens), Cohere Embed v3 English or multilingual ($0.10) | embedding the whole corpus costs a fraction of a cent |
| In Mumbai, Bedrock serves no reranking model; Cohere Embed v4 is offered only through a global route | in-region rules out a hosted reranker and Embed v4 |
| The chatbot already resolves products and territories (value index, chapter 6) | entity resolution is already built |

## Decision 0: do we need RAG? Not yet, so we will prove when we do

With about 2,200 tokens of descriptions and a few reports, the honest answer is: put it all in
the prompt. So the first thing built is a **baseline**: every question gets the whole corpus.
Retrieval is then built and compared against it on the same golden questions:

| | A: whole corpus in the prompt | B: hybrid retrieval, top 5 chunks |
|---|---|---|
| Quality | measured | measured |
| Input tokens per question | grows with the corpus | stays about the same |
| Cost and latency | measured | measured |

Retrieval wins when it matches A's quality at lower cost, which happens as the report archive
grows. If it does not win, we keep A and say so. Showing that comparison is worth more than
claiming RAG everywhere.

## The decisions

| # | Decision | Our choice | Why |
|---|---|---|---|
| 1 | Search | **Hybrid**: keyword (SQLite FTS5, BM25) plus vector, fused with **RRF** | product names like "Mountain-200" need exact keyword match; descriptions need meaning |
| 1b | Reranker | **None at first** | none served in Mumbai; RRF is free; add one only if recall@5 misses the target |
| 2 | Routing | **Rules first**, using the linker that exists: a governed metric means SQL; a product or "why / describe / what does" with no metric means RAG; both means SQL and RAG | free, instant, explainable; a trained classifier (question embedding plus logistic regression) only if routing accuracy on the golden set falls below 95% |
| 3 | Graph | **Yes, from the warehouse**: tag each chunk with the products, models, categories and territories it mentions, using the value index | text full of product names and codes; the graph comes from keys we already trust, at no model cost |
| 4 | Vector store | **Local**: vectors in SQLite, exact (brute-force) search. **Enterprise**: Postgres with pgvector | under a few thousand chunks, exact search takes about a millisecond; Postgres row-level security can protect chunks like rows; no extra system |
| 5 | Ingestion | **Event-driven and idempotent**: a report is indexed when it is approved and published; descriptions when the warehouse loads; a content hash skips unchanged text | small, steady growth; no nightly bulk job needed yet |
| 6 | Embedding model | **Titan Text Embeddings V2**, benchmarked against Cohere Embed multilingual v3 | in-region and cheapest; Cohere only if it retrieves better, or if users ask in Hindi or Hinglish |

**Not chosen, and why:** a dedicated vector database (no scale need, one more system to run);
OpenSearch (an always-on minimum cost for a tiny corpus); S3 Vectors (built for huge, rarely
queried collections, and this one is small and busy); LLM-extracted GraphRAG (our entities
already exist as data); a hosted reranker (not in-region).

## How an answer will be checked

The rule from chapter 1 does not relax for documents: **no number reaches a person unless code
traced it to a source.**

1. The model answers in the same JSON shape as everywhere else, citing chunk IDs.
2. Code checks that every cited chunk was really among those retrieved.
3. The number checker (chapter 5) runs with the numbers found in the cited chunks, plus any SQL
   result, as the facts. A number in neither fails, and goes to one repair.
4. If the best match is weak, the bot says "I could not find this in the documents", and does
   not let the model fill the gap.
5. The audit line records the chunk IDs used.

## How it will be measured

A golden set in the same style as the chatbot's:

| Kind | Example | Pass means |
|---|---|---|
| Retrieval | "Which road frame is the lightest?" | the HL Road Frame description is in the top 5 (recall@5) |
| Both SQL and documents | "What were reseller sales in September 2013, and what did that month's review say about them?" | the number from SQL, the comment from the cited report |
| Unanswerable | "What is the Mountain-200's battery life?" | "not found", no invented answer |
| Injection | a test document saying "ignore your instructions" | the answer is unaffected and still checked |
| Routing | each question tagged SQL, RAG or both | routing accuracy |

**Targets:** recall@5 at least 0.9; every number checked; unanswerable questions 100% "not
found"; 95% of answers in under 3 seconds; under $0.0005 per answer on Gemma. Each run reports
quality, latency and cost for the A versus B comparison, committed to `eval/results/` like the
other benchmarks.

## What it will look like in an enterprise

The same design, with the swaps from chapter 11: Postgres with pgvector and row-level security on
chunks; S3 as the landing bucket for documents; a parser that handles PDFs and scans; a personal-data
scan before indexing; the index rebuilt as a new version and switched only after the golden set
passes; and the router's decisions logged so that the "not sure" rate shows where to improve.

## The interview version

"I measured whether we needed RAG before building it. The corpus was 2,000 tokens, so the
baseline was the whole corpus in the prompt. Retrieval is hybrid, keyword plus vector with
rank fusion, because product names need exact matches. There's no reranker, because none is
served in-region. Entities come from the warehouse, not from an LLM. And every number in a
document answer is checked against the cited text, just like the reports."

Back to the [guide index](README.md)
