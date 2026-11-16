"""The value index: business names in a question become exact filters. No LLM.

    idx = build_value_index("samples/adventureworks.sqlite", g)
    found = idx.match("Internet sales of Mountain-200 Black, 38 in Northwest last 10 days")
    # → DimProduct.EnglishProductName = 'Mountain-200 Black, 38'
    #   DimSalesTerritory.SalesTerritoryRegion = 'Northwest'

Why it exists: schema linking finds *columns* ("region"), but a question names *values*
("Northwest", "Mountain-200 Black, 38"). A model guessing the spelling of a value writes
``WHERE region = 'North West'`` and gets zero rows, or worse, silently drops the filter. Here
the value is looked up in the data, the filter is exact, and the SQL is checked to apply it.

**What it reads.** Only the distinct values of the columns listed in ``specs/entities.yaml``
(product, model, category, colour, territory, state, reseller, promotion names), at most
``MAX_VALUES`` per column, over the read-only connection. No measures, no keys, no customer
columns. That list is the governance boundary: a column not listed is never read.

**How it matches.** Question and values are reduced to lower-case word stems
("Mountain-200 Black, 38" → ``mountain 200 black 38``). Every run of question words that
equals a value is a match; longer matches win and overlapping shorter ones are dropped
("Mountain-200 Black, 38" beats the model "Mountain-200" and the colour "Black"). When the
same words are a value of two columns, the column listed first in the entities file wins and
the other is kept as an alternative, shown to the user as an assumption.

**The knowledge graph.** ``add_to_graph`` writes each value as a ``value:`` node joined to its
column (``value_of``), and, where one entity table points to another by a foreign key, the
value hierarchy (``belongs_to``): product → subcategory → category, state → territory region.
That is the start of the knowledge graph the chatbot and RAG build on.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import yaml

from agentic_reporting.data import connect_ro
from agentic_reporting.schema_graph import ROOT, STOP, TIME_WORDS, stem, tokens

DEFAULT_ENTITIES = next((p for p in (ROOT / "specs" / "entities.yaml",
                                     Path("specs") / "entities.yaml") if p.is_file()),
                        ROOT / "specs" / "entities.yaml")
MAX_VALUES = 5000            # per column; more than this is not a name list, it is data
MAX_PAIRS = 20000            # per hierarchy edge set
MAX_SUGGESTIONS = 5
_WORD_RE = re.compile(r"[a-z0-9]+")


def norm(text: str) -> tuple[str, ...]:
    """Lower-case word stems, stop words KEPT (values are matched as contiguous phrases)."""
    return tuple(stem(w) for w in _WORD_RE.findall(str(text).lower()))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _matchable(key: tuple[str, ...]) -> bool:
    """A value too short or too common to be matched safely is not indexed."""
    if not key or len(" ".join(key)) < 3:
        return False
    if all(w in STOP or w in TIME_WORDS or w.isdigit() for w in key):
        return False
    return True


@dataclass
class Entity:
    column: str           # "table.column"
    label: str

    @property
    def table(self) -> str:
        return self.column.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.column.split(".", 1)[1]


@dataclass
class ValueMatch:
    column: str
    value: str
    label: str
    phrase: str                      # the question's words that matched
    start: int                       # word span in the question
    end: int
    alternatives: list[str] = field(default_factory=list)    # other "label: column" readings


@dataclass
class ValueIndex:
    entities: list[Entity]
    values: dict[str, list[str]]                                # column → distinct values
    lookup: dict[tuple[str, ...], list[tuple[int, str]]]        # key → [(entity order, value)]
    pairs: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    max_len: int = 1

    def label(self, column: str) -> str:
        return next(e.label for e in self.entities if e.column == column)

    def match(self, question: str) -> list[ValueMatch]:
        """Every value named in ``question``: longest first, no overlaps, config order on ties."""
        words = [stem(w) for w in _WORD_RE.findall(question.lower())]
        raw = _WORD_RE.findall(question.lower())
        taken = [False] * len(words)
        out: list[ValueMatch] = []
        for n in range(min(self.max_len, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                if any(taken[i:i + n]):
                    continue
                hits = self.lookup.get(tuple(words[i:i + n]))
                if not hits:
                    continue
                hits = sorted(hits)
                order, value = hits[0]
                ent = self.entities[order]
                alts = sorted({f"{self.entities[o].label}: {self.entities[o].column}"
                               for o, _ in hits[1:] if self.entities[o].column != ent.column})
                out.append(ValueMatch(ent.column, value, ent.label, " ".join(raw[i:i + n]),
                                      i, i + n, alts))
                taken[i:i + n] = [True] * n
        return sorted(out, key=lambda m: m.start)

    def suggest(self, phrase: str, n: int = MAX_SUGGESTIONS) -> list[dict[str, str]]:
        """Closest known values to words the bot did not understand (for a clarifying question)."""
        key = " ".join(norm(phrase))
        if not key:
            return []
        names = {" ".join(k): v for k, v in self.lookup.items()}
        close = difflib.get_close_matches(key, list(names), n=n, cutoff=0.6)
        if not close:      # one word right ("mountain"): values that contain it
            words = set(key.split()) - STOP
            close = sorted((k for k in names if words & set(k.split())), key=len)[:n]
        out = []
        for k in close:
            order, value = sorted(names[k])[0]
            out.append({"value": value, "label": self.entities[order].label,
                        "column": self.entities[order].column})
        return out

    def explained(self, matches: list[ValueMatch]) -> set[str]:
        """Word stems (schema-graph tokens) the matches account for."""
        return {t for m in matches for t in tokens(m.phrase)}


def load_entities(path: str | Path = DEFAULT_ENTITIES) -> list[Entity]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [Entity(e["column"], e["label"]) for e in data.get("entities", [])]


def build_value_index(db_path: str | Path, g: nx.DiGraph,
                      entities_path: str | Path = DEFAULT_ENTITIES) -> ValueIndex:
    """Distinct values of the listed entity columns that exist in this database."""
    ents = [e for e in load_entities(entities_path) if f"column:{e.column}" in g]
    values: dict[str, list[str]] = {}
    lookup: dict[tuple[str, ...], list[tuple[int, str]]] = defaultdict(list)
    pairs: dict[tuple[str, str], list[tuple[str, str]]] = {}
    conn = connect_ro(db_path)
    try:
        for order, e in enumerate(ents):
            rows = conn.execute(
                f"SELECT DISTINCT {_q(e.name)} FROM {_q(e.table)} WHERE {_q(e.name)} IS NOT NULL "
                f"ORDER BY 1 LIMIT {MAX_VALUES + 1}").fetchall()
            vals = [str(r[0]) for r in rows[:MAX_VALUES]]
            values[e.column] = vals
            for v in vals:
                key = norm(v)
                if _matchable(key) and (order, v) not in lookup[key]:
                    lookup[key].append((order, v))
        # Hierarchy: entity table A points to entity table B by a foreign key.
        joins = [d for _, d in g.nodes(data=True) if d["kind"] == "join"]
        for a in ents:
            for b in ents:
                if a.table == b.table:
                    continue
                j = next((d for d in joins if d["left_table"] == a.table
                          and d["right_table"] == b.table), None)
                if not j:
                    continue
                rows = conn.execute(
                    f"SELECT DISTINCT a.{_q(a.name)}, b.{_q(b.name)} FROM {_q(a.table)} AS a "
                    f"JOIN {_q(b.table)} AS b ON a.{_q(j['left_column'])} = "
                    f"b.{_q(j['right_column'])} WHERE a.{_q(a.name)} IS NOT NULL "
                    f"AND b.{_q(b.name)} IS NOT NULL LIMIT {MAX_PAIRS}").fetchall()
                pairs[(a.column, b.column)] = [(str(x), str(y)) for x, y in rows]
    finally:
        conn.close()
    max_len = max((len(k) for k in lookup), default=1)
    return ValueIndex(ents, values, dict(lookup), pairs, max_len)


def add_to_graph(g: nx.DiGraph, idx: ValueIndex) -> nx.DiGraph:
    """Values as nodes on their columns, and the belongs_to hierarchy between them."""
    for col, vals in idx.values.items():
        for v in vals:
            node = f"value:{col}={v}"
            g.add_node(node, kind="value", column=col, value=v, label=idx.label(col))
            g.add_edge(node, f"column:{col}", kind="value_of")
    for (ca, cb), rows in idx.pairs.items():
        for va, vb in rows:
            g.add_edge(f"value:{ca}={va}", f"value:{cb}={vb}", kind="belongs_to")
    return g


def filters_from(matches: list[ValueMatch]) -> list[dict[str, Any]]:
    """One filter per column: ``column IN (values)``, with the words that produced it."""
    by_col: dict[str, dict[str, Any]] = {}
    for m in matches:
        f = by_col.setdefault(m.column, {"column": m.column, "label": m.label, "values": [],
                                         "phrases": [], "alternatives": []})
        if m.value not in f["values"]:
            f["values"].append(m.value)
            f["phrases"].append(m.phrase)
        f["alternatives"] += [a for a in m.alternatives if a not in f["alternatives"]]
    return list(by_col.values())
