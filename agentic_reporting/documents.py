"""Document sources → chunks, for the RAG index. No LLM.

    chunks, quarantined = load_chunks("samples/adventureworks.sqlite")

Only the sources in ``specs/documents.yaml`` are read (the governance boundary for text, as
``specs/entities.yaml`` is for values). Every chunk has:

* a stable ``id`` (``product:road-150``, ``report:2013-09:summary``), so citations survive a
  rebuild;
* a ``title`` that makes it stand on its own when retrieved ("Product model Road-150 (Road
  Bikes, Bikes)"): the context comes from warehouse keys, not from a model;
* ``entities``: the warehouse values it is about (model, subcategory, category), so retrieval
  can filter by the products a question names (the knowledge-graph link);
* ``hash``: SHA-256 of title + text; unchanged text is never re-embedded.

**Screening.** Text that reads like an instruction to a model ("ignore your previous
instructions") is quarantined at ingestion and never indexed. That is a heuristic first line
against indirect prompt injection; the real defences come after it (the model has no tools,
its answer is checked against the cited text).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentic_reporting.data import connect_ro
from agentic_reporting.schema_graph import ROOT

DEFAULT_DOCUMENTS = next((p for p in (ROOT / "specs" / "documents.yaml",
                                      Path("specs") / "documents.yaml") if p.is_file()),
                         ROOT / "specs" / "documents.yaml")
INJECTION_RE = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override)\b[^.]{0,40}\b(?:instructions?|prompts?|rules)\b"
    r"|\bsystem prompt\b|\byou are now\b|\bnew instructions\b")
MAX_CHUNK_CHARS = 2000


@dataclass
class Chunk:
    id: str
    source: str
    title: str
    text: str
    entities: dict[str, str] = field(default_factory=dict)     # "Table.Column" → value

    @property
    def body(self) -> str:
        """What is embedded and searched: the title gives the text its context."""
        return f"{self.title}. {self.text}"

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def screen(text: str) -> str | None:
    """The instruction-like phrase in ``text``, or None."""
    m = INJECTION_RE.search(text)
    return m.group(0) if m else None


def _sql_chunks(src: dict[str, Any], db: str | Path) -> list[Chunk]:
    conn = connect_ro(db)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(src.get("requires", [])) <= have:
            return []
        cur = conn.execute(src["sql"])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        if not r.get("text"):
            continue
        out.append(Chunk(id=f"{src['id']}:{slug(r['name'])}", source=src["id"],
                         title=src["title"].format(**r), text=str(r["text"]).strip(),
                         entities={col: str(r[key]) for col, key in src.get("entities", {}).items()
                                   if r.get(key)}))
    return out


def _file_chunks(src: dict[str, Any], base: Path) -> list[Chunk]:
    folder = (base / src["folder"]).resolve()
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.md")):
        doc = path.stem
        text = path.read_text(encoding="utf-8")
        head = re.search(r"(?m)^# (.+)$", text)
        doc_title = head.group(1).strip() if head else doc
        for part in re.split(r"(?m)^## ", text)[1:]:
            heading, _, body = part.partition("\n")
            body = body.strip()[:MAX_CHUNK_CHARS]
            if body:
                out.append(Chunk(id=f"{src['id']}:{doc}:{slug(heading)}", source=src["id"],
                                 title=f"{doc_title}, {heading.strip()}", text=body))
    return out


def load_chunks(db: str | Path, documents_path: str | Path = DEFAULT_DOCUMENTS,
                base: Path = ROOT, extra: list[Chunk] | None = None,
                ) -> tuple[list[Chunk], list[dict[str, str]]]:
    """Chunks from every listed source that exists, plus ``extra``; and the quarantined ones."""
    data = yaml.safe_load(Path(documents_path).read_text(encoding="utf-8")) or {}
    chunks: list[Chunk] = []
    for src in data.get("sources", []):
        if src["kind"] == "sql":
            chunks += _sql_chunks(src, db)
        elif src["kind"] == "files":
            chunks += _file_chunks(src, base)
        else:
            raise ValueError(f"unknown document source kind {src['kind']!r}")
    chunks += extra or []
    kept, quarantined = [], []
    for c in chunks:
        hit = screen(c.body)
        if hit:
            quarantined.append({"id": c.id, "phrase": hit})
        else:
            kept.append(c)
    ids = [c.id for c in kept]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate chunk ids: {dupes[:5]}")
    return kept, quarantined
