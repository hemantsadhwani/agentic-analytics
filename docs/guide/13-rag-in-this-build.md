# 13. RAG in this build: the choices and what we measured

Chapter 12 gave the general decisions. This chapter applies them to agentic-analytics, with the
real numbers that drive each choice.

> **Status (16 Nov 2026): built and measured over the product descriptions.** The code is
> `agentic_reporting/documents.py`, `embeddings.py`, `doc_index.py` and `rag.py`; the chatbot
> routes to it in `chat.py`. Tests: `tests/test_rag.py`. Benchmark:
> `eval/results/rag_bench_gemma-3-12b_titan_2026-11-16.json`. **Not built yet:** the archive of
> published reports as a second source, and questions that need SQL and documents together.

## Start from the facts

| Fact (checked 16 Nov 2026) | Why it matters |
|---|---|
| The product catalogue gives 118 chunks (one per product model), about 3,400 tokens in all | tiny: it fits in any prompt |
| A published report is about 500 to 700 tokens; the archive grows every month | the corpus grows; a monthly review for each month of AdventureWorks history is about 37 reports |
| Gemma 3 models are built for up to 128,000 tokens of context | everything fits in the prompt today |
| In Mumbai, Bedrock serves embedding models in-region: Titan Text Embeddings V2 ($0.024 per million tokens), Cohere Embed v3 ($0.10, billed through AWS Marketplace) | embedding the whole corpus costs a fraction of a cent |
| In Mumbai, Bedrock serves no reranking model; Cohere Embed v4 is offered only through a global route | in-region rules out a hosted reranker and Embed v4 |
| The chatbot already resolves products and territories (value index, chapter 6) | entity resolution is already built |

## Decision 0: do we need RAG? We measured it

With a corpus this small, the honest first answer is: put it all in the prompt. So we built
that **baseline** first (A), then retrieval (B), and ran both on the same 19 golden questions,
three times each. Model: Gemma 3 12B. Embedder: Titan V2. Same answer checks in both.

| | A: whole corpus in the prompt | B: hybrid retrieval, top 5 chunks |
|---|---|---|
| Correct answers | 18 of 19, all three runs | **19 of 19**, all three runs |
| Right chunk in the top 5 (recall@5) | not applicable | 14 of 14 answerable questions |
| Unanswerable questions said "not found" | 5 of 5 | 5 of 5 |
| Input tokens per question | 5,967 | **401** |
| Cost per question | $0.000668 | **$0.000059** (11 times cheaper) |
| Typical latency (p50) | 542–573 ms | **437–489 ms** |
| Slow latency (p95) | 845–1,091 ms | 637–784 ms |
| One-time index build | none | 118 chunks, 3,383 tokens, $0.00008, 12.6 s |

**What A got wrong:** "Which bike is ridden by race winners?" was answered "Road bike", not
"Road-150". With 118 products in the prompt, the model found the right sentence but not the
right name. With 5 passages it named the product every time.

**Conclusion:** retrieval wins already: better quality, an eleventh of the cost, faster. Re-running
the sync embedded nothing (the content hash skipped every unchanged chunk).

## The decisions

| # | Decision | Our choice | Why |
|---|---|---|---|
| 1 | Search | **Hybrid**: keyword (SQLite FTS5, BM25) plus vector, fused with **RRF** | product names like "Mountain-200" need exact keyword match; descriptions need meaning |
| 1b | Reranker | **None** | none served in Mumbai; RRF is free; recall@5 was 14/14 without one |
| 2 | Routing | **Rules**, using the linker that exists: a governed metric means SQL; no metric means documents | free, instant, explainable; a trained classifier only if routing accuracy falls below 95% |
| 3 | Graph | **Yes, from the warehouse**: each chunk is tagged with its product model, subcategory and category; a named model or subcategory in the question narrows the search | the entities come from keys we already trust, at no model cost |
| 4 | Vector store | **Local**: vectors in SQLite, exact (brute-force) search. **Enterprise**: Postgres with pgvector | at 118 chunks, exact search takes about a millisecond; Postgres row-level security can protect chunks like rows; no extra system |
| 5 | Ingestion | **Idempotent sync**: a content hash skips unchanged text; changed chunks are re-embedded, deleted ones removed; instruction-like text is quarantined before indexing | small, steady growth; no nightly bulk job needed yet |
| 6 | Embedding model | **Titan Text Embeddings V2** | in-region, cheapest, and good enough: recall@5 14/14. Cohere was dropped (below) |

**Not chosen, and why:** a dedicated vector database (no scale need, one more system to run);
OpenSearch (an always-on minimum cost for a tiny corpus); S3 Vectors (built for huge, rarely
queried collections, and this one is small and busy); LLM-extracted GraphRAG (our entities
already exist as data); a hosted reranker (not in-region).

## Two lessons from the build

**1. A broad entity is noise, not a filter.** At first every entity in the question filtered the
search. "How many bikes does the Hitch Rack carry?" then searched only the *Bikes* category, and
the Hitch Rack (an accessory) was gone. Next we tried the category as a third ranked list in the
fusion. It still lost: being in that list added as much score as ranking first in a real search,
so every mediocre bike outranked the one chunk both searches put first. **The fix:** a product
model or subcategory filters; a category word (a quarter of the catalogue) is ignored. If a
filter leaves nothing, it is dropped, and the answer says so.

**2. Some models bring a procurement step.** Cohere Embed v3 scored the same as Titan (19/19,
recall 14/14, two runs), and built the index 11 times faster, because it takes 96 texts per
call where Titan takes one. Then AWS refused it: `INVALID_PAYMENT_INSTRUMENT`. Cohere on
Bedrock is billed through AWS Marketplace, which needs its own payment setup. Titan is billed as
normal Bedrock usage. So for one short query, Titan wins on cost (4 times cheaper), on equal
quality, with no extra contract. A single Titan query embedding takes about 83 ms (p50; p95
90 ms; `eval/results/embed_latency_2026-11-16.json`), about a sixth of a 500 ms answer. Batching
matters for bulk ingestion, not for one user's question.

## How an answer is checked

The rule from chapter 1 does not relax for documents: **no number reaches a person unless code
traced it to a source.**

1. The model answers in a fixed JSON shape: found or not, the text, and the chunk IDs it used.
2. Code checks that every cited chunk was really among those given. A made-up ID is rejected.
3. The number checker (chapter 5) runs with the numbers in the cited chunks as its facts.
   Product names such as "Road-150" are masked first, so their digits are not claims.
4. A miss gets one repair. If the repair fails too, the bot quotes the cited passage word for
   word instead of the model's text.
5. If the model says the passages do not contain the answer, the bot says "I could not find
   this in the documents", and nothing else.
6. The audit line records the route and the chunk IDs used.

## How it is measured

`python -m eval.rag_eval` runs the golden set (`eval/datasets/golden/rag_questions_adventureworks.json`):

| Kind | Example | Pass means |
|---|---|---|
| Answerable (14) | "Which road frame is the lightest?" | cites the right chunk and names the product |
| Unanswerable (5) | "What is the Mountain-200's battery life?" | "not found", no invented answer |
| Injection | a planted chunk saying "ignore all previous instructions" | quarantined at ingestion in every run; "Are helmets free today?" comes back "not found" |
| Retrieval | every answerable question | the right chunk is in the top 5 (recall@5) |

`--fake` runs the same set offline with a hashing embedder and a word-overlap stand-in for the
model. It proves the plumbing and the checks, not reading quality, and it gates CI.

**Targets and results:** recall@5 at least 0.9 (got 1.0); every number checked (yes);
unanswerable 100% "not found" (yes); 95% of answers under 3 seconds (slowest p95 784 ms); under
$0.0005 per answer (got $0.000059).

## What it will look like in an enterprise

The same design, with the swaps from chapter 11: Postgres with pgvector and row-level security on
chunks; S3 as the landing bucket for documents; a parser that handles PDFs and scans; a personal-data
scan before indexing; the index rebuilt as a new version and switched only after the golden set
passes; and the router's decisions logged so that the "not sure" rate shows where to improve.

## The interview version

"I measured whether we needed RAG before building it. The baseline put all 118 product
descriptions in the prompt. Retrieval beat it on all three counts: 19 of 19 correct against
18, an eleventh of the cost, and faster. Search is hybrid, keyword plus vector with rank
fusion, because product names need exact matches. There's no reranker, because none is served
in-region, and recall was already perfect. Entities come from the warehouse, not an LLM, and I
learned that a broad entity like a category must not filter. Every number in a document answer
is checked against the cited text, just like the reports."

Back to the [guide index](README.md)
