"""ADR-0007 step 5: the knowledge graph in Neo4j, queried with Cypher.

    python -m agentic_reporting.graphdb --db samples/adventureworks.sqlite          # load + check parity
    python -m agentic_reporting.graphdb --db samples/adventureworks.sqlite --check  # parity only

Needs the optional extra ``pip install -e ".[graphdb]"`` (the ``neo4j`` driver plus the
``[ontology]`` extra) and a running Neo4j 5. The connection comes only from the environment:
``NEO4J_URI``, ``NEO4J_USER``, ``NEO4J_PASSWORD`` (and optionally ``NEO4J_DATABASE``). No
password is stored in the repo; tests skip when the variables are unset.

**One source of truth.** The property graph is converted from the RDF graph that
``ontology.py`` builds (instances + SKOS vocabulary), and its labels and relationship types
come from the OWL TBox, which is generated from ``specs/entities.yaml``. Nothing here names a
business class or relation by hand:

* **nodes**: every IRI typed with an OWL class of the TBox. Labels = the class and its
  superclasses (``:Product:ProductTaxonomyLevel:BusinessEntity``), plus one *namespace* label
  (default ``:AgenticKG``) so a load owns exactly its nodes and never touches anything else in
  the database. Properties: ``iri`` (unique per namespace), ``name`` (the value, the chunk
  title, or the metric name), ``column``, ``matchKey`` (the value index's key),
  ``scheme``, ``topConcept``; chunks add ``source`` and ``contentHash``; metrics add their
  governed fields and ``synonyms``.
* **relationships**: every triple whose predicate is an OWL object property of the TBox, typed
  by the property's name in upper snake case (``ar:inProductSubcategory`` →
  ``:IN_PRODUCT_SUBCATEGORY``, ``ar:mentions`` → ``:MENTIONS``); and ``skos:narrower`` from an
  ambiguous metric word to the metrics it may mean (``:NARROWER``). ``skos:broader`` between
  values is not stored twice: every hierarchy property is a sub-property of it, so a roll-up
  walks the hierarchy types (``HIERARCHY``).
* ``skos:inScheme`` and ``skos:topConceptOf`` become the ``scheme`` and ``topConcept``
  properties; concept schemes are not nodes.

**Idempotent.** ``load`` MERGEs every node on ``iri`` and replaces its properties and labels,
MERGEs every relationship, then deletes the namespace's nodes and relationships that are not
in the plan. Re-running gives the same graph; ``diff`` compares the database with the plan
node by node and edge by edge (not only counts), so a corrupted edge is found.

The warehouse keeps the numbers; the graph keeps the map. No fact rows are loaded.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from rdflib import OWL, RDF, RDFS, SKOS, Graph

from agentic_reporting import ontology as onto

DEFAULT_NS = "AgenticKG"
BATCH = 1000
QUERY_TIMEOUT_S = 10.0
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def ident(name: str) -> str:
    """A label or relationship type safe to format into Cypher (letters, digits, underscore)."""
    if not _IDENT.match(name):
        raise ValueError(f"not a safe Cypher identifier: {name!r}")
    return name


def rel_type(local: str) -> str:
    """``inProductSubcategory`` → ``IN_PRODUCT_SUBCATEGORY`` (Cypher's convention)."""
    return ident(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", local).upper())


def local(iri: Any) -> str:
    s = str(iri)
    return s.split("#")[-1] if "#" in s else s.rsplit("/", 1)[-1]


# --- the schema: labels and relationship types, from the TBox -----------------------------

@dataclass(frozen=True)
class GraphSchema:
    labels: dict[str, list[str]]              # class → its labels (class first, then superclasses)
    rel_types: dict[str, tuple[str, str]]     # type → (from label, to label)
    hierarchy: tuple[str, ...]                # relationship types that are skos:broader links
    max_depth: int                            # longest roll-up chain, in hops
    properties: dict[str, list[str]] = field(default_factory=dict)   # label → property keys
    edges: tuple[tuple[str, str, str], ...] = ()                     # (from class, type, to class)
    present: frozenset[str] | None = None     # labels that have nodes in the loaded graph (prompt only)

    def all_labels(self) -> set[str]:
        return {lab for labs in self.labels.values() for lab in labs}

    def hierarchy_pattern(self) -> str:
        return "|".join(self.hierarchy)

    def chains(self) -> list[list[tuple[str, str]]]:
        """Every roll-up path from a leaf class: [(class, type to next), ..., (top class, "")]."""
        up: dict[str, list[tuple[str, str]]] = {}
        for a, t, b in self.edges:
            if t in self.hierarchy:
                up.setdefault(a, []).append((t, b))
        has_child = {b for ups in up.values() for _, b in ups}
        out: list[list[tuple[str, str]]] = []

        def walk(c: str, path: list[tuple[str, str]]) -> None:
            if c not in up:
                out.append([*path, (c, "")])
            for t, b in sorted(up.get(c, [])):
                walk(b, [*path, (c, t)])

        for leaf in sorted(set(up) - has_child):
            walk(leaf, [])
        return out

    def describe(self, present: set[str] | None = None) -> str:
        """The schema as a model sees it. ``present``: only the labels that have nodes."""
        present = present if present is not None else self.present

        def keep(cls: str) -> bool:
            return present is None or cls in present

        lines = ["Node labels (a node also carries every superclass label) and their properties:"]
        for cls, labs in sorted(self.labels.items()):
            if keep(cls):
                lines.append(f"  :{':'.join(labs)}  {{{', '.join(self.properties.get(cls, []))}}}")
        lines.append("Hierarchies, child -> parent (a child has at most one parent per level):")
        for chain in self.chains():
            if all(keep(c) for c, _ in chain):
                lines.append("  " + "".join(f"(:{c})" + (f"-[:{t}]->" if t else "") for c, t in chain))
        lines.append("Other relationships:")
        for a, t, b in sorted(self.edges):
            if t not in self.hierarchy and keep(a) and keep(b):
                lines.append(f"  (:{a})-[:{t}]->(:{b})")
        for t, (a, b) in sorted(self.rel_types.items()):
            if not any(e[1] == t for e in self.edges) and t not in self.hierarchy:
                lines.append(f"  (:{a})-[:{t}]->(:{b})")
        return "\n".join(lines)


VALUE_PROPS = ["iri", "name", "column", "matchKey", "scheme", "topConcept"]
CHUNK_PROPS = ["iri", "id", "name", "source", "contentHash"]
METRIC_PROPS = ["iri", "name", "label", "synonyms", "definition", "expression", "owner", "table", "grain",
                "timeColumn", "derivesFromColumn", "scheme", "topConcept"]
GROUP_PROPS = ["iri", "name", "synonyms", "clarifyingQuestion", "scheme", "topConcept"]


def class_labels(tbox: Graph) -> dict[str, list[str]]:
    """Every OWL class of the ontology → its labels: itself, then its superclasses up to the root."""
    out: dict[str, list[str]] = {}
    for c in tbox.subjects(RDF.type, OWL.Class):
        if not str(c).startswith(str(onto.AR)):
            continue
        chain, node = [], c
        while node is not None and str(node).startswith(str(onto.AR)):
            chain.append(ident(local(node)))
            node = tbox.value(node, RDFS.subClassOf)
        out[chain[0]] = chain
    return out


def build_schema(tbox: Graph | None = None) -> GraphSchema:
    tbox = tbox if tbox is not None else onto.build_tbox()
    labels = class_labels(tbox)
    rels: dict[str, tuple[str, str]] = {}
    hier: list[str] = []
    parent: dict[str, set[str]] = {}
    for p in sorted(tbox.subjects(RDF.type, OWL.ObjectProperty)):
        t = rel_type(local(p))
        rels[t] = (local(tbox.value(p, RDFS.domain)), local(tbox.value(p, RDFS.range)))
        if (p, RDFS.subPropertyOf, SKOS.broader) in tbox:
            hier.append(t)
    rels["NARROWER"] = ("MetricGroup", "Metric")
    # The longest roll-up chain: follow hierarchy properties class to class.
    spec = onto.load_spec()
    for r in spec.relations:
        if r.hierarchical:
            parent.setdefault(spec.column_class[r.child], set()).add(spec.column_class[r.to])

    def depth(c: str, seen: frozenset[str] = frozenset()) -> int:
        if c in seen:
            raise ValueError(f"class hierarchy cycle at {c}")
        return max((1 + depth(p, seen | {c}) for p in parent.get(c, ())), default=0)

    props = {c: VALUE_PROPS for c in labels}
    props.update({"Chunk": CHUNK_PROPS, "Metric": METRIC_PROPS, "MetricGroup": GROUP_PROPS})
    edges = {(spec.column_class[r.child], rel_type(r.prop), spec.column_class[r.to]) for r in spec.relations}
    return GraphSchema(labels, rels, tuple(hier), max((depth(c) for c in labels), default=0) or 1,
                       props, tuple(sorted(edges)))


# --- the plan: RDF → labelled property graph ------------------------------------------------

@dataclass
class Node:
    labels: tuple[str, ...]
    props: dict[str, Any]


@dataclass
class LPG:
    nodes: dict[str, Node]                          # iri → node
    rels: set[tuple[str, str, str]]                 # (type, from iri, to iri)

    def node_counts(self) -> Counter:
        return Counter(lab for n in self.nodes.values() for lab in n.labels)

    def rel_counts(self) -> Counter:
        return Counter(t for t, _, _ in self.rels)


def _str(x: Any) -> str | None:
    return None if x is None else str(x)


def lpg_from_rdf(g: Graph, tbox: Graph | None = None) -> LPG:
    """Convert an instance + vocabulary RDF graph into nodes and relationships (see module doc)."""
    tbox = tbox if tbox is not None else onto.build_tbox()
    labels = class_labels(tbox)
    scheme_name = {s: str(g.value(s, SKOS.prefLabel)) for s in g.subjects(RDF.type, SKOS.ConceptScheme)}
    nodes: dict[str, Node] = {}
    for s, cls in g.subject_objects(RDF.type):
        name = local(cls)
        if not str(cls).startswith(str(onto.AR)) or name not in labels:
            continue
        if str(s) in nodes:
            raise ValueError(f"{s} has two classes")
        iri = str(s)
        common = {"iri": iri, "scheme": scheme_name.get(g.value(s, SKOS.inScheme)),
                  "topConcept": g.value(s, SKOS.topConceptOf) is not None}
        if name == "Chunk":
            props = {"iri": iri, "id": unquote(iri.rsplit("/", 1)[-1]), "name": _str(g.value(s, RDFS.label)),
                     "source": _str(g.value(s, onto.AR.source)),
                     "contentHash": _str(g.value(s, onto.AR.contentHash))}
        elif name == "Metric":
            props = {**common, "name": _str(g.value(s, onto.AR.metricName)),
                     "label": _str(g.value(s, SKOS.prefLabel)),
                     "synonyms": sorted(str(x) for x in g.objects(s, SKOS.altLabel)),
                     "definition": _str(g.value(s, SKOS.definition)),
                     "expression": _str(g.value(s, onto.AR.expression)),
                     "owner": _str(g.value(s, onto.AR.owner)), "table": _str(g.value(s, onto.AR.table)),
                     "grain": _str(g.value(s, onto.AR.grain)),
                     "timeColumn": _str(g.value(s, onto.AR.timeColumn)),
                     "derivesFromColumn": sorted(str(x) for x in g.objects(s, onto.AR.derivesFromColumn))}
        elif name == "MetricGroup":
            props = {**common, "name": _str(g.value(s, SKOS.prefLabel)),
                     "synonyms": sorted(str(x) for x in g.objects(s, SKOS.altLabel)),
                     "clarifyingQuestion": _str(g.value(s, onto.AR.clarifyingQuestion))}
        else:
            props = {**common, "name": _str(g.value(s, SKOS.prefLabel)),
                     "column": _str(g.value(s, onto.AR.column)),
                     "matchKey": _str(g.value(s, SKOS.hiddenLabel))}
        nodes[iri] = Node(tuple(labels[name]), {k: v for k, v in props.items() if v is not None})
    object_props = {p for p in tbox.subjects(RDF.type, OWL.ObjectProperty)}
    hier = {p for p in object_props if (p, RDFS.subPropertyOf, SKOS.broader) in tbox}
    rels: set[tuple[str, str, str]] = set()
    for p in object_props:
        for a, b in g.subject_objects(p):
            for x in (a, b):
                if str(x) not in nodes:
                    raise ValueError(f"{local(p)} links to {x}, which is not a typed node")
            rels.add((rel_type(local(p)), str(a), str(b)))
    implied = {(str(b), str(a)) for p in hier for a, b in g.subject_objects(p)}
    for a, b in g.subject_objects(SKOS.narrower):
        if (str(a), str(b)) not in implied:
            rels.add(("NARROWER", str(a), str(b)))
    return LPG(nodes, rels)


def build_lpg(db: str | Path, **paths: Any) -> LPG:
    """The plan for one database: its instance graph plus the SKOS vocabulary (metrics)."""
    g = onto.build_instances(db, **paths)
    g += onto.build_vocabulary(paths.get("entities_path", onto.DEFAULT_ENTITIES),
                               paths.get("metrics_path", onto.DEFAULT_METRICS))
    return lpg_from_rdf(g, onto.build_tbox(paths.get("entities_path", onto.DEFAULT_ENTITIES)))


# --- connection -----------------------------------------------------------------------------

def env_config() -> dict[str, str] | None:
    """NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from the environment, or None if any is unset."""
    keys = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
    if not all(os.environ.get(k) for k in keys):
        return None
    return {"uri": os.environ["NEO4J_URI"], "user": os.environ["NEO4J_USER"],
            "password": os.environ["NEO4J_PASSWORD"], "database": os.environ.get("NEO4J_DATABASE", "neo4j")}


def connect(cfg: dict[str, str] | None = None) -> Any:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:   # pragma: no cover - exercised only without the extra
        raise ImportError('Neo4j needs the optional extra: pip install -e ".[graphdb]"') from exc
    cfg = cfg or env_config()
    if cfg is None:
        raise RuntimeError("set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    driver.verify_connectivity()
    return driver


def _db(cfg_db: str | None) -> str:
    return cfg_db or os.environ.get("NEO4J_DATABASE", "neo4j")


def _write(driver: Any, query: str, database: str | None = None, **params: Any) -> Any:
    return driver.execute_query(query, params, database_=_db(database))


def _read(driver: Any, query: str, database: str | None = None, **params: Any) -> list[Any]:
    from neo4j import Query, RoutingControl

    records, _, _ = driver.execute_query(Query(query, timeout=QUERY_TIMEOUT_S), params,
                                         database_=_db(database), routing_=RoutingControl.READ)
    return records


# --- load, diff, drop -----------------------------------------------------------------------

def constraint_names(ns: str) -> list[str]:
    return [f"{ns.lower()}_iri_unique"]


def ensure_schema(driver: Any, ns: str = DEFAULT_NS, database: str | None = None) -> None:
    """One uniqueness constraint per namespace (``iri``), and lookup indexes for the queries."""
    ns = ident(ns)
    _write(driver, f"CREATE CONSTRAINT {ns.lower()}_iri_unique IF NOT EXISTS "
                   f"FOR (n:{ns}) REQUIRE n.iri IS UNIQUE", database)
    for label, prop in (("BusinessEntity", "name"), ("BusinessEntity", "matchKey"),
                        ("BusinessEntity", "column"), ("Chunk", "id"), ("Metric", "name"),
                        ("MetricGroup", "name")):
        _write(driver, f"CREATE INDEX kg_{label.lower()}_{prop.lower()} IF NOT EXISTS "
                       f"FOR (n:{label}) ON (n.{prop})", database)


def _batches(rows: list[Any]) -> Any:
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def load(driver: Any, lpg: LPG, ns: str = DEFAULT_NS, database: str | None = None) -> dict[str, int]:
    """Write the plan into the namespace; idempotent (see module doc). Returns what changed."""
    ns = ident(ns)
    ensure_schema(driver, ns, database)
    rows = [{"iri": iri, "props": n.props, "labels": [ident(lab) for lab in n.labels]}
            for iri, n in sorted(lpg.nodes.items())]
    for batch in _batches(rows):
        _write(driver, f"""UNWIND $rows AS r
            MERGE (n:{ns} {{iri: r.iri}})
            SET n = r.props
            WITH n, r, [l IN labels(n) WHERE l <> '{ns}' AND NOT l IN r.labels] AS stale
            REMOVE n:$(stale)
            SET n:$(r.labels)""", database, rows=batch)
    plan_iris = sorted(lpg.nodes)
    deleted_nodes = _write(driver, f"""MATCH (n:{ns}) WHERE NOT n.iri IN $iris
        DETACH DELETE n RETURN count(*) AS n""", database, iris=plan_iris).records[0]["n"]
    by_type: dict[str, list[dict[str, str]]] = {}
    for t, a, b in sorted(lpg.rels):
        by_type.setdefault(ident(t), []).append({"a": a, "b": b})
    for t, pairs in by_type.items():
        for batch in _batches(pairs):
            _write(driver, f"""UNWIND $pairs AS p
                MATCH (a:{ns} {{iri: p.a}}), (b:{ns} {{iri: p.b}})
                MERGE (a)-[:{t}]->(b)""", database, pairs=batch)
    deleted_rels = _write(driver, f"""MATCH (a:{ns})-[r]->(b:{ns})
        WHERE NOT type(r) IN $types OR NOT (type(r) + ' ' + a.iri + ' ' + b.iri) IN $keys
        DELETE r RETURN count(*) AS n""", database, types=sorted(by_type),
                          keys=sorted(f"{t} {a} {b}" for t, a, b in lpg.rels)).records[0]["n"]
    return {"nodes": len(lpg.nodes), "relationships": len(lpg.rels), "deleted_nodes": deleted_nodes,
            "deleted_relationships": deleted_rels}


def read_graph(driver: Any, ns: str = DEFAULT_NS, database: str | None = None) -> LPG:
    """Everything in the namespace, back as a plan (labels without the namespace label)."""
    ns = ident(ns)
    nodes = {}
    for r in _read(driver, f"MATCH (n:{ns}) RETURN n.iri AS iri, labels(n) AS labels, "
                           f"properties(n) AS props", database):
        nodes[r["iri"]] = Node(tuple(sorted(lab for lab in r["labels"] if lab != ns)), dict(r["props"]))
    rels = {(r["t"], r["a"], r["b"]) for r in _read(
        driver, f"MATCH (a:{ns})-[r]->(b:{ns}) RETURN type(r) AS t, a.iri AS a, b.iri AS b", database)}
    dangling = _read(driver, f"MATCH (a:{ns})-[r]-(b) WHERE NOT b:{ns} RETURN count(r) AS n", database)
    if dangling[0]["n"]:
        raise ValueError(f"{dangling[0]['n']} relationship(s) leave the namespace {ns}")
    return LPG(nodes, rels)


def diff(expected: LPG, actual: LPG, limit: int = 10) -> list[str]:
    """Differences between two graphs: counts per label and type, then node and edge sets."""
    out: list[str] = []
    for kind, e, a in (("label", expected.node_counts(), actual.node_counts()),
                       ("type", expected.rel_counts(), actual.rel_counts())):
        for k in sorted(set(e) | set(a)):
            if e[k] != a[k]:
                out.append(f"{kind} {k}: expected {e[k]}, found {a[k]}")
    norm_e = {i: (tuple(sorted(n.labels)), n.props) for i, n in expected.nodes.items()}
    norm_a = {i: (tuple(sorted(n.labels)), n.props) for i, n in actual.nodes.items()}
    out += [f"node missing: {i}" for i in sorted(set(norm_e) - set(norm_a))][:limit]
    out += [f"node not in plan: {i}" for i in sorted(set(norm_a) - set(norm_e))][:limit]
    out += [f"node differs: {i}" for i in sorted(set(norm_e) & set(norm_a)) if norm_e[i] != norm_a[i]][:limit]
    out += [f"edge missing: {r}" for r in sorted(expected.rels - actual.rels)][:limit]
    out += [f"edge not in plan: {r}" for r in sorted(actual.rels - expected.rels)][:limit]
    return out


def drop(driver: Any, ns: str, database: str | None = None) -> None:
    """Delete a namespace's nodes and its constraint (the shared lookup indexes stay)."""
    ns = ident(ns)
    _write(driver, f"MATCH (n:{ns}) DETACH DELETE n", database)
    for name in constraint_names(ns):
        _write(driver, f"DROP CONSTRAINT {name} IF EXISTS", database)


# --- Cypher queries: the twins of the SPARQL helpers in ontology.py --------------------------

class CypherKG:
    """Read-only Cypher over one namespace. Each method mirrors a SPARQL helper in ontology.py."""

    def __init__(self, driver: Any, ns: str = DEFAULT_NS, database: str | None = None,
                 schema: GraphSchema | None = None) -> None:
        self.driver, self.ns, self.database = driver, ident(ns), database
        self.schema = schema or build_schema()

    def rows(self, query: str, **params: Any) -> list[Any]:
        return _read(self.driver, query, self.database, **params)

    def _label(self, cls: str) -> str:
        if cls not in self.schema.all_labels():
            raise ValueError(f"unknown class {cls!r}")
        return f"{ident(cls)}:{self.ns}"

    def _type(self, prop: str) -> str:
        t = rel_type(prop)
        if t not in self.schema.rel_types:
            raise ValueError(f"unknown relationship {prop!r}")
        return t

    def members(self, cls: str, ancestor_cls: str, ancestor: str) -> set[str]:
        """Values of ``cls`` that roll up (any depth) to ``ancestor`` (SPARQL ``skos:broader+``)."""
        q = (f"MATCH (x:{self._label(cls)})-[:{self.schema.hierarchy_pattern()}*1..{self.schema.max_depth}]->"
             f"(a:{self._label(ancestor_cls)} {{name: $name}}) RETURN DISTINCT x.name AS v")
        return {r["v"] for r in self.rows(q, name=ancestor)}

    def count_by_parent(self, cls: str, prop: str) -> dict[str, int]:
        q = (f"MATCH (x:{self._label(cls)})-[:{self._type(prop)}]->(p:{self.ns}) "
             f"RETURN p.name AS parent, count(DISTINCT x) AS n")
        return {r["parent"]: r["n"] for r in self.rows(q)}

    def related_under(self, cls: str, prop: str, ancestor_cls: str, ancestor: str) -> set[str]:
        """Values of ``cls`` whose ``prop`` target is ``ancestor`` or rolls up to it."""
        q = (f"MATCH (x:{self._label(cls)})-[:{self._type(prop)}]->(:{self.ns})"
             f"-[:{self.schema.hierarchy_pattern()}*0..{self.schema.max_depth}]->"
             f"(a:{self._label(ancestor_cls)} {{name: $name}}) RETURN DISTINCT x.name AS v")
        return {r["v"] for r in self.rows(q, name=ancestor)}

    def resolve_name(self, phrase: str) -> set[tuple[str, str]]:
        """(column, exact value) for every value whose match key equals the phrase's."""
        q = (f"MATCH (x:BusinessEntity:{self.ns}) WHERE x.matchKey = $key "
             f"RETURN x.column AS c, x.name AS v")
        return {(r["c"], r["v"]) for r in self.rows(q, key=" ".join(onto.norm(phrase)))}

    def ambiguous_names(self) -> set[str]:
        q = (f"MATCH (x:BusinessEntity:{self.ns}) WHERE x.matchKey IS NOT NULL "
             f"WITH x.matchKey AS k, count(DISTINCT x.column) AS c WHERE c > 1 RETURN k")
        return {r["k"] for r in self.rows(q)}

    def resolve_metric(self, phrase: str) -> set[str]:
        """Metric names a word names: directly (label, synonym, name) or through an ambiguous group."""
        q = (f"MATCH (m:Metric:{self.ns}) WHERE toLower(m.label) = $p OR toLower(m.name) = $p "
             f"OR any(s IN m.synonyms WHERE toLower(s) = $p) RETURN m.name AS name "
             f"UNION MATCH (g:MetricGroup:{self.ns})-[:NARROWER]->(m:Metric:{self.ns}) "
             f"WHERE toLower(g.name) = $p OR any(s IN g.synonyms WHERE toLower(s) = $p) "
             f"RETURN m.name AS name")
        return {r["name"] for r in self.rows(q, p=phrase.lower())}

    def chunks_about(self, cls: str, value: str) -> set[str]:
        """IRIs of the chunks tagged with this value."""
        q = (f"MATCH (c:Chunk:{self.ns})-[:MENTIONS]->(e:{self._label(cls)} {{name: $v}}) "
             f"RETURN DISTINCT c.iri AS c")
        return {r["c"] for r in self.rows(q, v=value)}

    def chunks_under(self, ancestor_cls: str, ancestor: str) -> set[str]:
        """Multi-hop: chunks that mention any value at or below ``ancestor`` in the hierarchy."""
        q = (f"MATCH (c:Chunk:{self.ns})-[:MENTIONS]->(:{self.ns})"
             f"-[:{self.schema.hierarchy_pattern()}*0..{self.schema.max_depth}]->"
             f"(a:{self._label(ancestor_cls)} {{name: $name}}) RETURN DISTINCT c.iri AS c")
        return {r["c"] for r in self.rows(q, name=ancestor)}

    def related_under_with(self, cls: str, prop: str, ancestor_cls: str, ancestor: str,
                           attr_prop: str, attr: str) -> set[str]:
        """Multi-hop with two constraints: resellers under a territory group with a business type."""
        q = (f"MATCH (x:{self._label(cls)})-[:{self._type(prop)}]->(:{self.ns})"
             f"-[:{self.schema.hierarchy_pattern()}*0..{self.schema.max_depth}]->"
             f"(a:{self._label(ancestor_cls)} {{name: $name}}) "
             f"MATCH (x)-[:{self._type(attr_prop)}]->(t:{self.ns} {{name: $attr}}) "
             f"RETURN DISTINCT x.name AS v")
        return {r["v"] for r in self.rows(q, name=ancestor, attr=attr)}

    def counts(self) -> tuple[Counter, Counter]:
        """Nodes per label and relationships per type, counted by Cypher."""
        nodes = Counter({r["l"]: r["n"] for r in self.rows(
            f"MATCH (n:{self.ns}) UNWIND labels(n) AS l WITH l, count(*) AS n WHERE l <> '{self.ns}' "
            f"RETURN l, n")})
        rels = Counter({r["t"]: r["n"] for r in self.rows(
            f"MATCH (:{self.ns})-[r]->(:{self.ns}) RETURN type(r) AS t, count(*) AS n")})
        return nodes, rels


# --- CLI ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load one database's knowledge graph into Neo4j "
                                             "(env NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD) and check parity.")
    ap.add_argument("--db", required=True, help="the warehouse (SQLite) whose graph to load")
    ap.add_argument("--ns", default=DEFAULT_NS, help="namespace label (default %(default)s)")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if the database differs")
    args = ap.parse_args(argv)
    if env_config() is None:
        print("set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD (nothing is read from the repo)")
        return 2
    plan = build_lpg(args.db)
    driver = connect()
    try:
        if not args.check:
            print(f"loaded {args.db} into :{args.ns}: {load(driver, plan, args.ns)}")
        problems = diff(plan, read_graph(driver, args.ns))
        nodes, rels = CypherKG(driver, args.ns).counts()
    finally:
        driver.close()
    print(f":{args.ns}: {len(plan.nodes)} nodes, {len(plan.rels)} relationships; "
          + ", ".join(f"{k} {v}" for k, v in sorted(rels.items())))
    if problems:
        print("differs from the plan:\n  " + "\n  ".join(problems))
        return 1
    print("parity: the database equals the plan (every node, label, property and edge)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
