"""The document index: keyword search + vector search, fused by rank. One SQLite file.

    idx = DocIndex(".agentic_reporting/docs.sqlite", embedder)
    stats = idx.sync(chunks)          # add new, re-embed changed, delete removed; idempotent
    hits = idx.retrieve("Which road frame is the lightest?", k=5,
                        entities={"DimProductSubcategory.EnglishProductSubcategoryName": ["Road Frames"]})

**Hybrid search.** Keyword search (SQLite FTS5, BM25 ranking, Porter stemming) finds exact
names and codes ("Mountain-200"); vector search (cosine over unit vectors) finds meaning
("lightest frame" ~ "best quality aluminum frame"). Each gives a ranked list of 20; reciprocal
rank fusion scores every chunk ``sum(1 / (60 + rank))`` over the lists it appears in. No model,
no score scaling, deterministic. There is no reranker: Bedrock serves none in ap-south-1.

**Entities (the knowledge-graph link)**, found in the question by the value index:

* a named product model or subcategory is a **filter**: only chunks about it are searched
  ("the Mountain-200's battery life" must not be answered from the Taillight's batteries);
* a category word (Bikes, Components, Clothing, Accessories) is **ignored** for retrieval: it
  covers a quarter of the catalogue and is usually just a noun. Measured on the golden set
  (16 Nov): as a filter, "How many bikes does the Hitch Rack carry?" lost the Hitch Rack (an
  accessory); as a third ranked list in the fusion it still lost, because appearing in that
  list added as much as ranking first in a real search, so every mediocre bike outranked a
  chunk that both searches ranked first.

If a filter leaves nothing, it is dropped and the result says so.

**Exact vector search.** Vectors are stored as float32 blobs and compared one by one in plain
Python. At a few thousand chunks that takes milliseconds and needs no extra dependency; the
enterprise version is Postgres with pgvector and an HNSW index (``docs/guide/13``).

**One embedding model per index.** The model id is stored; opening the index with another
embedder rebuilds it, because vectors from different models cannot be compared.
"""

from __future__ import annotations

import json
import re
import sqlite3
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_reporting.documents import Chunk
from agentic_reporting.embeddings import Embedder, cosine
from agentic_reporting.schema_graph import STOP, TIME_WORDS

RRF_K = 60
CANDIDATES = 20
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
    entities TEXT NOT NULL, hash TEXT NOT NULL, vector BLOB NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id UNINDEXED, body,
    tokenize = 'porter unicode61');
"""


@dataclass
class Hit:
    chunk: Chunk
    score: float                      # reciprocal rank fusion score
    keyword_rank: int | None          # 1-based, None if keyword search missed it
    vector_rank: int | None
    cosine: float


@dataclass
class Retrieval:
    hits: list[Hit]
    keyword_hits: int                 # how many chunks keyword search found at all
    max_cosine: float
    filtered_by: dict[str, list[str]] = field(default_factory=dict)
    filter_dropped: bool = False


def _blob(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def _vec(b: bytes) -> list[float]:
    a = array("f")
    a.frombytes(b)
    return a.tolist()


def fts_query(question: str) -> str:
    """Every content word, quoted, OR-ed: recall first, BM25 ranks."""
    words = [w.lower() for w in _WORD_RE.findall(question)]
    words = [w for w in dict.fromkeys(words) if w not in STOP and w not in TIME_WORDS]
    return " OR ".join(f'"{w}"' for w in words)


def rrf(*ranked: list[str], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for lst in ranked:
        for rank, cid in enumerate(lst, 1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


class DocIndex:
    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(DDL)
        model = self._meta("embed_model")
        if model is not None and model != embedder.model_id:
            self.conn.executescript("DELETE FROM chunks; DELETE FROM chunks_fts; DELETE FROM meta;")
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)",
                          (embedder.model_id,))
        self.conn.commit()
        self._cache: list[tuple[Chunk, list[float]]] | None = None

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()

    # --- ingestion -----------------------------------------------------------------------

    def sync(self, chunks: list[Chunk]) -> dict[str, Any]:
        """Make the index hold exactly ``chunks``. Unchanged text is never re-embedded."""
        have = dict(self.conn.execute("SELECT id, hash FROM chunks").fetchall())
        want = {c.id: c for c in chunks}
        todo = [c for c in chunks if have.get(c.id) != c.hash]
        gone = [cid for cid in have if cid not in want]
        t0, c0 = self.embedder.tokens, self.embedder.cost_usd
        vecs = self.embedder.embed([c.body for c in todo], kind="document") if todo else []
        for c, v in zip(todo, vecs, strict=True):
            self.conn.execute("DELETE FROM chunks_fts WHERE id = ?", (c.id,))
            self.conn.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
                              (c.id, c.source, c.title, c.text, json.dumps(c.entities),
                               c.hash, _blob(v)))
            self.conn.execute("INSERT INTO chunks_fts VALUES (?, ?)", (c.id, c.body))
        for cid in gone:
            self.conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
            self.conn.execute("DELETE FROM chunks_fts WHERE id = ?", (cid,))
        self.conn.commit()
        self._cache = None
        return {"added": sum(1 for c in todo if c.id not in have),
                "updated": sum(1 for c in todo if c.id in have),
                "deleted": len(gone), "unchanged": len(chunks) - len(todo),
                "embed_tokens": self.embedder.tokens - t0,
                "embed_cost_usd": round(self.embedder.cost_usd - c0, 8),
                "embed_model": self.embedder.model_id}

    # --- reading -------------------------------------------------------------------------

    def all(self) -> list[tuple[Chunk, list[float]]]:
        if self._cache is None:
            rows = self.conn.execute(
                "SELECT id, source, title, text, entities, vector FROM chunks ORDER BY id")
            self._cache = [(Chunk(r[0], r[1], r[2], r[3], json.loads(r[4])), _vec(r[5]))
                           for r in rows]
        return self._cache

    def chunks(self) -> list[Chunk]:
        return [c for c, _ in self.all()]

    def _allowed(self, entities: dict[str, list[str]] | None) -> set[str] | None:
        if not entities:
            return None
        return {c.id for c, _ in self.all()
                if any(c.entities.get(col) in vals for col, vals in entities.items())}

    def keyword(self, question: str, allowed: set[str] | None = None,
                n: int = CANDIDATES) -> list[str]:
        q = fts_query(question)
        if not q:
            return []
        rows = self.conn.execute(
            "SELECT id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts)",
            (q,)).fetchall()
        ids = [r[0] for r in rows if allowed is None or r[0] in allowed]
        return ids[:n]

    def vector(self, qvec: list[float], allowed: set[str] | None = None,
               n: int = CANDIDATES) -> list[tuple[str, float]]:
        scored = [(c.id, cosine(qvec, v)) for c, v in self.all()
                  if allowed is None or c.id in allowed]
        return sorted(scored, key=lambda x: -x[1])[:n]

    def retrieve(self, question: str, k: int = 5,
                 entities: dict[str, list[str]] | None = None) -> Retrieval:
        allowed = self._allowed(entities)
        dropped = False
        if allowed is not None and not allowed:
            allowed, dropped = None, True
        qvec = self.embedder.embed([question], kind="query")[0]
        kw = self.keyword(question, allowed)
        vec = self.vector(qvec, allowed)
        cos = dict(vec)
        fused = rrf(kw, [cid for cid, _ in vec])
        by_id = {c.id: c for c in self.chunks()}
        order = sorted(fused, key=lambda cid: (-fused[cid], cid))[:k]
        hits = [Hit(by_id[cid], round(fused[cid], 6),
                    kw.index(cid) + 1 if cid in kw else None,
                    next((i for i, (x, _) in enumerate(vec, 1) if x == cid), None),
                    round(cos.get(cid, 0.0), 4)) for cid in order]
        return Retrieval(hits, len(kw), round(max(cos.values(), default=0.0), 4),
                         filtered_by={} if dropped else dict(entities or {}),
                         filter_dropped=dropped)
