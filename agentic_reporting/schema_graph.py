"""The schema graph: a map of the warehouse for schema linking. The rows stay in the warehouse.

    g = build_schema_graph("samples/sales.sqlite")          # tables, columns, joins, metrics
    r = link("Which region had the highest net revenue last month?", g)
    r.tables, r.join_path, r.metrics, r.confidence

**Build** reads only SQLite metadata (``sqlite_master``, ``PRAGMA table_info``,
``PRAGMA foreign_key_list``) over the read-only connection. It never reads a data row.
Nodes (``kind``):

* ``table:<t>`` and ``column:<t>.<c>`` (type, primary key);
* ``join:<t>.<c>=<t2>.<c2>``: a join key, from a declared foreign key or inferred when two
  tables share a ``*_id`` column that is the primary key of one of them. The left side
  holds the key (many rows), the right side is the table it points to (one row);
* ``metric:<name>``: a governed metric from ``specs/metrics.yaml`` (expression, grain,
  time column, owner), loaded only if its table and columns exist in this database;
* ``term:<phrase>``: a metric synonym.

Edges (``kind``): ``has_column`` (table → column), ``joins_on`` (join → both columns),
``derives_from`` (metric → column or metric), ``synonym_of`` (term → metric).

**Link** is deterministic and has no LLM and no embeddings. Candidates come from lexical
overlap between the question and table names, column names, metric names and synonyms
(lower-cased, CamelCase and ``_`` split, plural ``s`` stripped). The best-scoring metric is
expanded along ``derives_from``. Every candidate table is then connected to the anchor table
(the metric's table) by the shortest path that follows joins **from the many side to the one
side only** (fact → dimension → parent dimension). A path that would go back from a
dimension to another fact table would repeat rows and inflate the sum (a fan trap), so that
table is reported ``unreachable`` instead. ``confidence`` is a documented heuristic, not a
probability.

A word that names several governed metrics ("sales" when the warehouse has internet and
reseller sales) is listed under ``ambiguous:`` in the metrics file; if no specific metric
matched, ``link`` returns ``clarify`` with the options instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentic_reporting.data import connect_ro
from agentic_reporting.spec import FORBIDDEN

ROOT = Path(__file__).resolve().parent.parent
# The repo's specs/ in a checkout or editable install; ./specs/ when installed elsewhere (Docker).
DEFAULT_METRICS = next((p for p in (ROOT / "specs" / "metrics.yaml", Path("specs") / "metrics.yaml")
                        if p.is_file()), ROOT / "specs" / "metrics.yaml")
DEFAULT_GRAPH_OUT = Path(".agentic_reporting") / "schema_graph.json"

# Words that carry no schema meaning. Kept short and explicit so linking stays explainable.
STOP = frozenset("""
a an the of in on for to by per and or with from at as is are was were be been did do does
had has have what which who whom how many much we our us me show list give get got tell
most highest lowest top least best worst largest smallest biggest earned made each all any
there their it its than that were did can could would please about across
include included includes including contain contains containing during between versus vs
compare compared breakdown split overall total we go went look see
""".split())
# Time words: not linked to a node, but they count as understood (the SQL writer handles
# "last month" with the metric's time column).
TIME_WORDS = frozenset("""
last latest previous prior current this month monthly week weekly year yearly quarter day
daily today yesterday
""".split())
# Column-name parts too generic to link on by themselves.
GENERIC = frozenset({"id", "name", "at", "to", "of", "the", "is", "pct", "key", "english",
                     "alternate"})
# Star-schema prefixes: ``DimProduct`` is the product table, ``FactInternetSales`` internet sales.
TABLE_PREFIXES = frozenset({"dim", "fact"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE)

# confidence = W_METRIC·[metric found] + W_JOINED·[every candidate table joined]
#            + W_COVERAGE·(share of content words explained by a node or a time word)
W_METRIC, W_JOINED, W_COVERAGE = 0.4, 0.3, 0.3
PATH_EDGES = frozenset({"has_column", "joins_on"})


# --- tokens ------------------------------------------------------------------------------

def stem(word: str) -> str:
    """Crude singular: ``categories``→``category``, ``orders``→``order``, ``sales``→``sale``."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def split_camel(text: str) -> str:
    """``EnglishProductName`` → ``English Product Name``; ``order_date`` is unchanged."""
    return _CAMEL_RE.sub(" ", text)


def tokens(text: str) -> list[str]:
    """Lower-case word stems, stop words removed (``_``, ``-`` and CamelCase split)."""
    return [stem(w) for w in _TOKEN_RE.findall(split_camel(text).lower().replace("_", " "))
            if w not in STOP]


def is_key(column: str) -> bool:
    """A surrogate or foreign key (``customer_id``, ``ProductKey``): linked only through joins."""
    return column == "id" or column.endswith("_id") or column.endswith("Key")


def is_label(column: str) -> bool:
    return column.endswith("_name") or (column.endswith("Name") and not is_key(column))


# --- governed metrics --------------------------------------------------------------------

class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    table: str
    expression: str
    grain: str
    time_column: str | None = None
    derives_from: list[str] = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    owner: str
    description: str

    @field_validator("expression")
    @classmethod
    def _expression(cls, v: str) -> str:
        if bad := _FORBIDDEN_RE.search(v):
            raise ValueError(f"forbidden keyword {bad.group(1).upper()} in metric expression")
        if ";" in v or "--" in v:
            raise ValueError("metric expression must be one expression, no ';' or comments")
        return v.strip()


class Ambiguous(BaseModel):
    """A word that names several governed metrics; the bot asks which one is meant."""
    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(min_length=1)
    options: list[str] = Field(min_length=2)
    ask: str


def load_ambiguous(path: str | Path = DEFAULT_METRICS) -> list[Ambiguous]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [Ambiguous.model_validate(a) for a in data.get("ambiguous", [])]


def load_metrics(path: str | Path = DEFAULT_METRICS) -> list[Metric]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    metrics = [Metric.model_validate(m) for m in data.get("metrics", [])]
    names = [m.name for m in metrics]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"duplicate metric names: {dupes}")
    return metrics


# --- build -------------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _join_id(lt: str, lc: str, rt: str, rc: str) -> str:
    return f"join:{lt}.{lc}={rt}.{rc}"


def build_schema_graph(db_path: str | Path, metrics_path: str | Path = DEFAULT_METRICS) -> nx.DiGraph:
    """Tables, columns, join keys and governed metrics of one SQLite database, as a graph."""
    g = nx.DiGraph(db=Path(db_path).name, metrics_file=Path(metrics_path).name)
    conn = connect_ro(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        cols: dict[str, list[tuple[str, str, bool]]] = {}
        raw_fks: list[tuple[str, str, str, str | None]] = []
        for t in tables:
            info = conn.execute(f"PRAGMA table_info({_q(t)})").fetchall()
            cols[t] = [(r[1], (r[2] or "").upper(), bool(r[5])) for r in info]
            # (id, seq, parent table, child column, parent column or NULL = parent's PK, ...)
            raw_fks += [(t, r[3], r[2], r[4])
                        for r in conn.execute(f"PRAGMA foreign_key_list({_q(t)})").fetchall()]
    finally:
        conn.close()
    fks = []
    for t, c, parent, pcol in raw_fks:
        pcol = pcol or next((pc for pc, _, pk in cols.get(parent, []) if pk), None)
        if pcol:
            fks.append((t, c, parent, pcol))

    for t in tables:
        g.add_node(f"table:{t}", kind="table", name=t)
        for c, ctype, pk in cols[t]:
            g.add_node(f"column:{t}.{c}", kind="column", table=t, name=c, type=ctype, pk=pk)
            g.add_edge(f"table:{t}", f"column:{t}.{c}", kind="has_column")

    def add_join(lt: str, lc: str, rt: str, rc: str, source: str) -> None:
        jid = _join_id(lt, lc, rt, rc)
        if jid in g or _join_id(rt, rc, lt, lc) in g:
            return
        g.add_node(jid, kind="join", left_table=lt, left_column=lc, right_table=rt,
                   right_column=rc, source=source)
        g.add_edge(jid, f"column:{lt}.{lc}", kind="joins_on")
        g.add_edge(jid, f"column:{rt}.{rc}", kind="joins_on")

    for lt, lc, rt, rc in fks:
        if f"column:{lt}.{lc}" in g and f"column:{rt}.{rc}" in g:
            add_join(lt, lc, rt, rc, "foreign_key")
    # Inferred: a shared *_id column that is the primary key of exactly one side.
    for t in tables:
        for c, _, pk in cols[t]:
            if pk or not c.endswith("_id"):
                continue
            for t2 in tables:
                if t2 != t and any(c2 == c and pk2 for c2, _, pk2 in cols[t2]):
                    add_join(t, c, t2, c, "name")

    loaded: list[Metric] = []
    for m in load_metrics(metrics_path):
        cols_ok = all(ref.startswith("metric:") or f"column:{ref}" in g for ref in m.derives_from)
        time_ok = m.time_column is None or f"column:{m.time_column}" in g
        if f"table:{m.table}" in g and cols_ok and time_ok:
            loaded.append(m)
    names = {m.name for m in loaded}
    loaded = [m for m in loaded
              if all(ref[len("metric:"):] in names for ref in m.derives_from if ref.startswith("metric:"))]
    for m in loaded:
        g.add_node(f"metric:{m.name}", kind="metric", **m.model_dump())
    for m in loaded:
        for ref in m.derives_from:
            target = ref if ref.startswith("metric:") else f"column:{ref}"
            g.add_edge(f"metric:{m.name}", target, kind="derives_from")
        for syn in m.synonyms:
            term = f"term:{syn.lower()}"
            g.add_node(term, kind="term", phrase=syn.lower())
            g.add_edge(term, f"metric:{m.name}", kind="synonym_of")
    g.graph["ambiguous"] = [a.model_dump() for a in load_ambiguous(metrics_path)
                            if all(o in names for o in a.options)]
    return g


def to_json(g: nx.DiGraph) -> dict[str, Any]:
    """Stable, human-readable JSON: sorted nodes and edges, every attribute kept."""
    return {
        "graph": dict(g.graph),
        "nodes": [{"id": n, **g.nodes[n]} for n in sorted(g.nodes)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in sorted(g.edges(data=True))],
    }


def write_json(g: nx.DiGraph, path: str | Path = DEFAULT_GRAPH_OUT) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_json(g), indent=2) + "\n", encoding="utf-8")
    return path


def summary(g: nx.DiGraph) -> str:
    kinds = [d["kind"] for _, d in g.nodes(data=True)]
    edges = [d["kind"] for _, _, d in g.edges(data=True)]
    n = ", ".join(f"{kinds.count(k)} {k}s" for k in ("table", "column", "join", "metric", "term"))
    e = ", ".join(f"{edges.count(k)} {k}" for k in ("has_column", "joins_on", "derives_from",
                                                    "synonym_of"))
    return f"{n}; edges: {e}"


# --- link --------------------------------------------------------------------------------

@dataclass
class JoinStep:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    source: str

    def __str__(self) -> str:
        return (f"{self.left_table}.{self.left_column} = {self.right_table}.{self.right_column}"
                f"  ({self.source.replace('_', ' ')})")


@dataclass
class LinkResult:
    question: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)          # "table.column"
    join_path: list[JoinStep] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)          # primary first, then its inputs
    confidence: float = 0.0
    column_types: dict[str, str] = field(default_factory=dict)
    metric_definitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)    # node → why it was linked
    unexplained: list[str] = field(default_factory=list)      # content words no node matched
    unreachable: list[str] = field(default_factory=list)      # candidate tables with no join
    clarify: dict[str, Any] | None = None                     # {"ask", "phrase", "options"}
    filters: list[dict[str, Any]] = field(default_factory=list)   # resolved values, applied =
    time_range: dict[str, Any] | None = None                  # resolved dates, time column
    data_as_of: str | None = None                             # latest date in the metric data

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def subset(self) -> dict[str, Any]:
        """Exactly what the SQL writer is allowed to see: nothing outside the link."""
        return {
            "tables": {t: {c.split(".", 1)[1]: self.column_types.get(c, "")
                           for c in self.columns if c.split(".", 1)[0] == t}
                       for t in self.tables},
            "join_path": [f"{j.left_table}.{j.left_column} = {j.right_table}.{j.right_column}"
                          for j in self.join_path],
            "metrics": {n: {k: d[k] for k in ("label", "expression", "grain", "time_column",
                                                "description")}
                        for n, d in self.metric_definitions.items()},
            **({"filters": [{"column": f["column"], "values": f["values"]}
                            for f in self.filters]} if self.filters else {}),
            **({"time_range": {k: self.time_range[k] for k in ("column", "start", "end")}}
               if self.time_range else {}),
            **({"data_as_of": self.data_as_of} if self.data_as_of else {}),
        }


def _metric_closure(g: nx.DiGraph, name: str) -> list[str]:
    """``name`` then every metric it derives from (depth first, no repeats)."""
    out: list[str] = []
    stack = [f"metric:{name}"]
    while stack:
        node = stack.pop(0)
        if node in out:
            continue
        out.append(node)
        stack += [v for _, v, d in g.out_edges(node, data=True)
                  if d["kind"] == "derives_from" and g.nodes[v]["kind"] == "metric"]
    return [n[len("metric:"):] for n in out]


def link(question: str, g: nx.DiGraph, *, metric: str | None = None,
         filter_columns: list[str] | None = None, explained_words: set[str] | None = None,
         ) -> LinkResult:
    """Deterministic schema linking: candidates by lexical overlap, joins by shortest path.

    ``metric`` forces the primary metric (the user's answer to a clarifying question).
    ``filter_columns`` (``table.column``) are columns a resolved value sits in: their tables
    join the candidates. ``explained_words`` are word stems already understood elsewhere
    (a resolved value, a resolved time phrase).
    """
    q = tokens(question)
    qset = set(q)
    res = LinkResult(question=question)
    explained: set[str] = set(qset & TIME_WORDS) | (set(explained_words or ()) & qset)

    # 1. Metrics: a name or synonym matches if all its word stems are in the question.
    best: tuple[int, int, str, str] | None = None     # (score, -yaml order, name, phrase)
    metric_nodes = [n for n, d in g.nodes(data=True) if d["kind"] == "metric"]
    for order, node in enumerate(metric_nodes):
        d = g.nodes[node]
        for phrase in [d["name"].replace("_", " "), *d["synonyms"]]:
            pt = set(tokens(phrase))
            if pt and pt <= qset:
                explained |= pt
                cand = (len(pt), -order, d["name"], phrase)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    if metric is not None:
        if f"metric:{metric}" not in g:
            raise ValueError(f"unknown metric {metric!r} for this database")
        explained |= {w for a in g.graph.get("ambiguous", []) if metric in a["options"]
                      for p in a["phrases"] for w in tokens(p) if w in qset}
        best = (99, 0, metric, "chosen by the user")
    elif best is None:
        for a in g.graph.get("ambiguous", []):
            hit = next((p for p in a["phrases"] if (pt := set(tokens(p))) and pt <= qset), None)
            if hit:
                res.clarify = {"ask": a["ask"], "phrase": hit, "options": [
                    {"metric": o, "label": g.nodes[f"metric:{o}"]["label"]}
                    for o in a["options"]]}
                explained |= set(tokens(hit))
                break
    metric_words = set(tokens(best[3])) if best and metric is None else set()

    candidate_tables: list[str] = []
    columns: list[str] = []

    def add_table(t: str) -> None:
        if t not in candidate_tables:
            candidate_tables.append(t)

    def add_column(tc: str) -> None:
        if tc not in columns:
            columns.append(tc)

    for fc in filter_columns or []:
        if f"column:{fc}" in g:
            add_table(fc.split(".", 1)[0])
            add_column(fc)
            res.evidence.setdefault(f"column:{fc}", "a value in the question")

    if best:
        res.evidence[f"metric:{best[2]}"] = f"matched {best[3]!r}"
        for name in _metric_closure(g, best[2]):
            d = g.nodes[f"metric:{name}"]
            res.metrics.append(name)
            res.metric_definitions[name] = {k: d[k] for k in (
                "label", "expression", "grain", "time_column", "description", "owner")}
            add_table(d["table"])
            for ref in d["derives_from"]:
                if not ref.startswith("metric:"):
                    add_table(ref.split(".", 1)[0])
                    add_column(ref)
            if d["time_column"]:
                add_column(d["time_column"])

    # 2. Tables and columns by name. Key columns (*_id) are linked only through joins. A
    #    column links only if every significant part of its name is in the question
    #    ("unit price", not just "units"); a part equal to its own table's name ("customer"
    #    in customers.customer_name) and generic parts (id, name, at) do not count.
    lexical_hits: dict[str, int] = {}
    table_words: set[str] = set()
    for node, d in g.nodes(data=True):
        if d["kind"] != "table":
            continue
        tt = set(tokens(d["name"])) - TABLE_PREFIXES
        if tt and tt <= metric_words:      # "reseller sales" names a metric, not DimReseller
            continue
        if tt and tt <= qset:
            explained |= tt
            table_words |= tt
            add_table(d["name"])
            lexical_hits[d["name"]] = lexical_hits.get(d["name"], 0) + len(tt)
            res.evidence[node] = f"table name {d['name']!r}"
    col_hits: list[tuple[str, dict[str, Any], set[str]]] = []
    for node, d in g.nodes(data=True):
        if d["kind"] != "column" or is_key(d["name"]):
            continue
        own = set(tokens(d["table"])) - TABLE_PREFIXES
        ct = {t for t in tokens(d["name"]) if t not in GENERIC and t not in own}
        hit = ct if ct and ct <= qset else set()     # every significant part, not one
        if not hit or hit <= TIME_WORDS:            # "month" is a time phrase, not DimDate
            continue
        if hit <= table_words and d["table"] not in candidate_tables:
            continue                                # "product category" is DimProductCategory
        col_hits.append((node, d, hit))
    for node, d, hit in col_hits:
        if any(hit < other for _, _, other in col_hits):
            continue                                # "type" loses to "business type"
        explained |= hit
        add_table(d["table"])
        add_column(f"{d['table']}.{d['name']}")
        lexical_hits[d["table"]] = lexical_hits.get(d["table"], 0) + len(hit)
        res.evidence.setdefault(node, f"column word {sorted(hit)[0]!r}")

    # 3. Join path: shortest path from the anchor along joins, many side → one side only.
    if candidate_tables:
        anchor = (g.nodes[f"metric:{res.metrics[0]}"]["table"] if res.metrics else
                  min(lexical_hits, key=lambda t: (-lexical_hits[t], t)) if lexical_hits
                  else candidate_tables[0])
        tg = nx.DiGraph()
        tg.add_node(anchor)
        for node, d in g.nodes(data=True):
            if d["kind"] == "join":
                tg.add_edge(d["left_table"], d["right_table"], join=node)
        if not res.metrics:          # a lookup with no measure cannot fan out a sum
            tg = tg.to_undirected()
        tables = [anchor]
        for t in candidate_tables:
            if t == anchor:
                continue
            try:
                path = nx.shortest_path(tg, anchor, t)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                res.unreachable.append(t)
                continue
            for a, b in zip(path, path[1:], strict=False):
                for tn in (a, b):
                    if tn not in tables:
                        tables.append(tn)
                        res.evidence.setdefault(f"table:{tn}", "on the join path")
                d = g.nodes[tg.edges[a, b]["join"]]
                step = JoinStep(d["left_table"], d["left_column"], d["right_table"],
                                d["right_column"], d["source"])
                if step not in res.join_path:
                    res.join_path.append(step)
                    add_column(f"{step.left_table}.{step.left_column}")
                    add_column(f"{step.right_table}.{step.right_column}")
        res.tables = tables
        # A table linked only by its name gets its label column(s) (*_name, *Name; else its
        # text columns) so the answer can say which team or region, not just which key.
        for t in tables:
            if not any(c.split(".", 1)[0] == t and not is_key(c.split(".", 1)[1])
                       for c in columns):
                cols_t = [g.nodes[n] for n in g.successors(f"table:{t}")]
                labels = [c["name"] for c in cols_t if is_label(c["name"])] or [
                    c["name"] for c in cols_t if not is_key(c["name"]) and c["type"] == "TEXT"]
                for name in labels:
                    add_column(f"{t}.{name}")
        res.columns = [c for c in columns if c.split(".", 1)[0] in tables]
        res.column_types = {c: g.nodes[f"column:{c}"]["type"] for c in res.columns}

    res.unexplained = [t for t in dict.fromkeys(q) if t not in explained]
    coverage = 1.0 if not qset else len(qset & explained) / len(qset)
    if res.tables:
        res.confidence = round(W_METRIC * bool(res.metrics) + W_JOINED * (not res.unreachable)
                               + W_COVERAGE * coverage, 2)
    return res


def format_link(r: LinkResult) -> str:
    lines = [f"linked  confidence {r.confidence:.2f}"
             + (f"  (unexplained words: {', '.join(r.unexplained)})" if r.unexplained else "")]
    lines.append(f"  tables   {', '.join(r.tables) or '-'}")
    lines.append(f"  columns  {', '.join(r.columns) or '-'}")
    lines.append("  joins    " + ("; ".join(str(j) for j in r.join_path) or "- (single table)"))
    for i, name in enumerate(r.metrics):
        d = r.metric_definitions[name]
        tag = "metric  " if i == 0 else "  input "
        lines.append(f"  {tag} {name} = {d['expression']}  [owner: {d['owner']}]")
    if not r.metrics:
        lines.append("  metric   - (no governed metric matched)")
    if r.unreachable:
        lines.append(f"  unreachable (no join path): {', '.join(r.unreachable)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the schema graph of a SQLite DB and write JSON.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--metrics", default=str(DEFAULT_METRICS))
    ap.add_argument("--out", default=str(DEFAULT_GRAPH_OUT))
    args = ap.parse_args(argv)
    g = build_schema_graph(args.db, args.metrics)
    out = write_json(g, args.out)
    print(f"schema graph of {args.db}: {summary(g)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
