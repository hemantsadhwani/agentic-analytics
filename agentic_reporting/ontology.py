"""The ontology layer (ADR-0007): the knowledge graph in W3C standards, generated from the specs.

    python -m agentic_reporting.ontology                     # regenerate ontology/*.ttl from specs/
    python -m agentic_reporting.ontology --check             # fail if ontology/*.ttl is stale
    python -m agentic_reporting.ontology --db samples/adventureworks.sqlite   # + instances, SHACL

Needs the optional extra: ``pip install -e ".[ontology]"`` (rdflib, pyshacl). Nothing on the
default path imports this module.

**One source of truth.** Every triple comes from ``specs/entities.yaml``, ``specs/metrics.yaml``,
``specs/documents.yaml`` or the warehouse's master data. Nothing is written twice by hand: the
committed ``ontology/*.ttl`` files are generated, and a test fails when they drift from the specs.

* ``ontology/agentic-analytics.ttl`` (OWL): the entity classes and their subclass tree, an object
  property per declared hierarchy level (``ar:inProductSubcategory``, a sub-property of
  ``skos:broader``) and per declared relation (``ar:hasColour``, ``ar:locatedIn``), and the
  datatype properties of metrics, entities and document chunks.
* ``ontology/vocabulary.ttl`` (SKOS): the concept schemes, every governed metric as a
  ``skos:Concept`` (label, synonyms as ``skos:altLabel``, definition, owner, expression), and each
  ambiguous word ("sales") as a broader concept whose ``skos:narrower`` are the metrics it may mean.
* ``ontology/shapes.ttl`` (SHACL): what a valid graph looks like. Generated from the entity spec
  (one parent per value, the parent of the right class, functional relations) plus fixed rules
  (one label per concept, labels unique per class, no hierarchy cycles, every chunk's entity
  tags resolve to a typed node).
* the instance graph (ABox), built from one database and not committed: every value the value
  index reads (``values.py``) becomes a concept of its class, with ``skos:prefLabel`` (the exact
  value), ``skos:hiddenLabel`` (the value index's match key) and ``ar:column``; the declared
  hierarchy and relations come from the warehouse (a foreign key, reused from the value index,
  or the same row); every document chunk with the entities it mentions.

The warehouse keeps the numbers; the graph keeps the map. No fact rows are loaded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

try:
    from rdflib import OWL, RDF, RDFS, SH, SKOS, XSD, Graph, Literal, Namespace, URIRef
except ImportError as exc:   # pragma: no cover - exercised only without the extra
    raise ImportError('the ontology layer needs the optional extra: pip install -e ".[ontology]"') from exc

from agentic_reporting.data import connect_ro
from agentic_reporting.documents import DEFAULT_DOCUMENTS, load_chunks
from agentic_reporting.schema_graph import (
    DEFAULT_METRICS,
    ROOT,
    build_schema_graph,
    load_ambiguous,
    load_metrics,
    split_camel,
)
from agentic_reporting.values import DEFAULT_ENTITIES, MAX_PAIRS, _matchable, _q, build_value_index, norm

BASE = "https://github.com/hemantsadhwani/agentic-analytics/"
AR = Namespace(BASE + "ontology#")
ARS = Namespace(BASE + "ontology/shapes#")
ID = BASE + "id/"
ONTOLOGY_IRI = URIRef(BASE + "ontology")
ROOT_CLASS = "BusinessEntity"
LANG = "en"
DEFAULT_OUT = ROOT / "ontology"
FILES = {"tbox": "agentic-analytics.ttl", "vocabulary": "vocabulary.ttl", "shapes": "shapes.ttl"}
DEFAULT_INSTANCES = Path(".agentic_reporting") / "kg"
HASH_PATTERN = "^[0-9a-f]{64}$"

# Metric fields (schema_graph.Metric) → RDF. A test fails if Metric gains a field not mapped here.
METRIC_FIELDS: dict[str, tuple[URIRef, str]] = {
    "name": (AR.metricName, "string"),
    "label": (SKOS.prefLabel, "lang"),
    "table": (AR.table, "string"),
    "expression": (AR.expression, "string"),
    "grain": (AR.grain, "string"),
    "time_column": (AR.timeColumn, "string"),
    "derives_from": (AR.derivesFrom, "derives"),        # metric:x → ar:derivesFrom; else column
    "synonyms": (SKOS.altLabel, "lang"),
    "owner": (AR.owner, "string"),
    "description": (SKOS.definition, "lang"),
}


# --- the spec ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Relation:
    prop: str                # local name: inProductSubcategory, hasColour
    child: str               # entity column
    to: str                  # entity column
    functional: bool
    required: bool
    hierarchical: bool       # a broader link (sub-property of skos:broader)


@dataclass
class OntologySpec:
    classes: dict[str, dict[str, Any]]           # name → label, comment, subclass_of, scheme, columns
    column_class: dict[str, str]                 # entity column → class
    relations: list[Relation] = field(default_factory=list)
    digest: str = ""

    def ancestors(self, cls: str) -> list[str]:
        out = [cls]
        while (parent := self.classes[out[-1]]["subclass_of"]) is not None:
            out.append(parent)
        return out

    def scheme_of(self, cls: str) -> str:
        return next(c for c in self.ancestors(cls) if self.classes[c]["scheme"])

    def common_ancestor(self, classes: set[str]) -> str:
        chains = [self.ancestors(c) for c in sorted(classes)]
        return next(c for c in chains[0] if all(c in ch for ch in chains[1:]))

    def properties(self) -> dict[str, dict[str, Any]]:
        """prop → range class, domain classes, functional, hierarchical (one entry per property)."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.relations:
            p = out.setdefault(r.prop, {"range": self.column_class[r.to], "domain": set(),
                                        "functional": True, "hierarchical": r.hierarchical})
            p["domain"].add(self.column_class[r.child])
            p["functional"] = p["functional"] and r.functional
        return out


def _digest(data: Any) -> str:
    """Stable across line endings and key order: the parsed YAML, not the bytes."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def load_spec(path: str | Path = DEFAULT_ENTITIES) -> OntologySpec:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    classes: dict[str, dict[str, Any]] = {}
    for c in data.get("classes", []):
        classes[c["name"]] = {"label": c["label"], "comment": c.get("comment", ""),
                              "subclass_of": c.get("subclass_of"), "scheme": c.get("scheme"),
                              "columns": []}
    column_class: dict[str, str] = {}
    entities = data.get("entities", [])
    for e in entities:
        cls, parent = e.get("class"), e.get("subclass_of")
        if not cls:
            raise ValueError(f"entity {e['column']} has no class")
        if cls in classes:
            if parent and classes[cls]["subclass_of"] != parent:
                raise ValueError(f"class {cls} declared under {classes[cls]['subclass_of']} and {parent}")
        else:
            classes[cls] = {"label": e["label"], "comment": "", "subclass_of": parent,
                            "scheme": None, "columns": []}
        classes[cls]["columns"].append(e["column"])
        column_class[e["column"]] = cls
    roots = [n for n, c in classes.items() if c["subclass_of"] is None]
    if roots != [ROOT_CLASS]:
        raise ValueError(f"exactly one root class {ROOT_CLASS!r} expected, got {roots}")
    for name, c in classes.items():
        seen = {name}
        p = c["subclass_of"]
        while p is not None:
            if p not in classes:
                raise ValueError(f"class {name}: unknown parent class {p!r}")
            if p in seen:
                raise ValueError(f"class hierarchy has a cycle at {p}")
            seen.add(p)
            p = classes[p]["subclass_of"]
    spec = OntologySpec(classes, column_class, digest=_digest(data))
    for e in entities:
        if not e.get("broader") and "broader_required" in e:
            raise ValueError(f"{e['column']}: broader_required without broader")
        rels = []
        if e.get("broader"):
            rels.append((f"in{column_class.get(e['broader'], '?')}", e["broader"], True,
                         bool(e.get("broader_required", True)), True))
        for r in e.get("relations", []):
            rels.append((r["property"], r["to"], bool(r.get("functional", False)), False, False))
        for prop, to, functional, required, hier in rels:
            if to not in column_class:
                raise ValueError(f"{e['column']}: {to} is not an entity column")
            spec.relations.append(Relation(prop, e["column"], to, functional, required, hier))
    for cls in column_class.values():
        if not any(classes[c]["scheme"] for c in spec.ancestors(cls)):
            raise ValueError(f"class {cls}: no concept scheme on it or any parent class")
    for prop, p in spec.properties().items():
        ranges = {spec.column_class[r.to] for r in spec.relations if r.prop == prop}
        if len(ranges) > 1:
            raise ValueError(f"property {prop} points to several classes: {sorted(ranges)}")
        if p["hierarchical"] != all(r.hierarchical for r in spec.relations if r.prop == prop):
            raise ValueError(f"property {prop} is both a hierarchy and a relation")
    return spec


def entity_iri(cls: str, value: str) -> URIRef:
    """Injective: two different values never share an IRI (``quote`` keeps every character)."""
    return URIRef(f"{ID}{cls}/{quote(value, safe='')}")


def scheme_iri(name: str) -> URIRef:
    return URIRef(f"{ID}scheme/{name}")


def metric_iri(name: str) -> URIRef:
    return URIRef(f"{ID}metric/{name}")


def group_iri(phrase: str) -> URIRef:
    return URIRef(f"{ID}metric-group/{quote(phrase.replace(' ', '-'), safe='')}")


def chunk_iri(chunk_id: str) -> URIRef:
    return URIRef(f"{ID}chunk/{quote(chunk_id, safe='')}")


def _graph() -> Graph:
    g = Graph()
    for prefix, ns in (("ar", AR), ("ars", ARS), ("skos", SKOS), ("owl", OWL), ("sh", SH),
                       ("xsd", XSD), ("rdfs", RDFS)):
        g.bind(prefix, ns)
    return g


def _en(text: str) -> Literal:
    return Literal(text, lang=LANG)


# --- TBox (OWL) --------------------------------------------------------------------------

def build_tbox(entities_path: str | Path = DEFAULT_ENTITIES) -> Graph:
    spec = load_spec(entities_path)
    g = _graph()
    g.add((ONTOLOGY_IRI, RDF.type, OWL.Ontology))
    g.add((ONTOLOGY_IRI, RDFS.label, _en("Agentic analytics ontology")))
    g.add((ONTOLOGY_IRI, RDFS.comment, _en(
        "Generated from specs/entities.yaml by agentic_reporting.ontology. Do not edit by hand.")))
    g.add((ONTOLOGY_IRI, OWL.versionInfo, Literal(f"entities spec {spec.digest}")))

    def cls(name: str, label: str, comment: str, parent: URIRef) -> None:
        g.add((AR[name], RDF.type, OWL.Class))
        g.add((AR[name], RDFS.label, _en(label)))
        if comment:
            g.add((AR[name], RDFS.comment, _en(comment)))
        g.add((AR[name], RDFS.subClassOf, parent))

    for name, c in spec.classes.items():
        cls(name, c["label"], c["comment"] or ("Values of " + ", ".join(c["columns"]) + "."),
            AR[c["subclass_of"]] if c["subclass_of"] else SKOS.Concept)
        for col in c["columns"]:
            g.add((AR[name], AR.sourceColumn, Literal(col)))
        if c["scheme"]:
            g.add((AR[name], AR.conceptScheme, scheme_iri(name)))
    cls("Metric", "governed metric", "One agreed definition of a business number (specs/metrics.yaml).",
        SKOS.Concept)
    cls("MetricGroup", "ambiguous metric word",
        "A word that names several governed metrics; the assistant asks which one is meant.", SKOS.Concept)
    g.add((AR.Chunk, RDF.type, OWL.Class))
    g.add((AR.Chunk, RDFS.label, _en("document chunk")))
    g.add((AR.Chunk, RDFS.comment, _en("A retrievable passage (specs/documents.yaml).")))

    def prop(name: str, kind: URIRef, label: str, domain: URIRef | None, rng: URIRef | None) -> None:
        g.add((AR[name], RDF.type, kind))
        g.add((AR[name], RDFS.label, _en(label)))
        if domain is not None:
            g.add((AR[name], RDFS.domain, domain))
        if rng is not None:
            g.add((AR[name], RDFS.range, rng))

    for name, p in sorted(spec.properties().items()):
        rng = p["range"]
        prop(name, OWL.ObjectProperty,
             f"in {spec.classes[rng]['label']}" if p["hierarchical"] else split_camel(name).lower(),
             AR[spec.common_ancestor(p["domain"])], AR[rng])
        if p["hierarchical"]:
            g.add((AR[name], RDFS.subPropertyOf, SKOS.broader))
        if p["functional"]:
            g.add((AR[name], RDF.type, OWL.FunctionalProperty))
    prop("mentions", OWL.ObjectProperty, "mentions", AR.Chunk, AR[ROOT_CLASS])
    prop("derivesFrom", OWL.ObjectProperty, "derives from", AR.Metric, AR.Metric)
    prop("column", OWL.DatatypeProperty, "warehouse column", AR[ROOT_CLASS], XSD.string)
    for f, (p, kind) in METRIC_FIELDS.items():
        if kind == "string":
            prop(str(p).split("#")[1], OWL.DatatypeProperty, f.replace("_", " "), AR.Metric, XSD.string)
    prop("derivesFromColumn", OWL.DatatypeProperty, "derives from column", AR.Metric, XSD.string)
    prop("clarifyingQuestion", OWL.DatatypeProperty, "clarifying question", AR.MetricGroup, XSD.string)
    prop("source", OWL.DatatypeProperty, "document source", AR.Chunk, XSD.string)
    prop("contentHash", OWL.DatatypeProperty, "content hash (SHA-256)", AR.Chunk, XSD.string)
    prop("sourceColumn", OWL.AnnotationProperty, "source column", None, None)
    prop("conceptScheme", OWL.AnnotationProperty, "concept scheme", None, None)
    return g


# --- SKOS vocabulary ---------------------------------------------------------------------

def build_vocabulary(entities_path: str | Path = DEFAULT_ENTITIES,
                     metrics_path: str | Path = DEFAULT_METRICS) -> Graph:
    spec = load_spec(entities_path)
    g = _graph()
    for name, c in spec.classes.items():
        if c["scheme"]:
            g.add((scheme_iri(name), RDF.type, SKOS.ConceptScheme))
            g.add((scheme_iri(name), SKOS.prefLabel, _en(c["scheme"])))
    ms = scheme_iri("Metric")
    g.add((ms, RDF.type, SKOS.ConceptScheme))
    g.add((ms, SKOS.prefLabel, _en("Governed metrics")))
    metrics_data = yaml.safe_load(Path(metrics_path).read_text(encoding="utf-8")) or {}
    g.add((ms, OWL.versionInfo, Literal(f"metrics spec {_digest(metrics_data)}")))
    grouped: set[str] = set()
    for a in load_ambiguous(metrics_path):
        grp = group_iri(a.phrases[0])
        g.add((grp, RDF.type, AR.MetricGroup))
        g.add((grp, SKOS.prefLabel, _en(a.phrases[0])))
        for phrase in a.phrases[1:]:
            g.add((grp, SKOS.altLabel, _en(phrase)))
        g.add((grp, AR.clarifyingQuestion, Literal(a.ask)))
        g.add((grp, SKOS.inScheme, ms))
        g.add((grp, SKOS.topConceptOf, ms))
        for o in a.options:
            g.add((grp, SKOS.narrower, metric_iri(o)))
            g.add((metric_iri(o), SKOS.broader, grp))
            grouped.add(o)
    for m in load_metrics(metrics_path):
        node = metric_iri(m.name)
        g.add((node, RDF.type, AR.Metric))
        g.add((node, SKOS.inScheme, ms))
        if m.name not in grouped:
            g.add((node, SKOS.topConceptOf, ms))
        for f, (p, kind) in METRIC_FIELDS.items():
            raw = getattr(m, f)
            for v in (raw if isinstance(raw, list) else [raw]):
                if v is None:
                    continue
                if kind == "derives":
                    if v.startswith("metric:"):
                        g.add((node, AR.derivesFrom, metric_iri(v[len("metric:"):])))
                    else:
                        g.add((node, AR.derivesFromColumn, Literal(v)))
                else:
                    g.add((node, p, _en(v) if kind == "lang" else Literal(v)))
    return g


# --- SHACL shapes ------------------------------------------------------------------------

def build_shapes(entities_path: str | Path = DEFAULT_ENTITIES) -> Graph:
    spec = load_spec(entities_path)
    g = _graph()

    def node_shape(name: str, target: URIRef, message: str) -> URIRef:
        s = ARS[name]
        g.add((s, RDF.type, SH.NodeShape))
        g.add((s, SH.targetClass, target))
        g.add((s, RDFS.comment, _en(message)))
        return s

    def prop_shape(owner: URIRef, name: str, path: URIRef, message: str, *, min_count: int | None = None,
                   max_count: int | None = None, cls: URIRef | None = None,
                   datatype: URIRef | None = None, pattern: str | None = None) -> None:
        s = ARS[name]
        g.add((owner, SH.property, s))
        g.add((s, RDF.type, SH.PropertyShape))
        g.add((s, SH.path, path))
        g.add((s, SH.message, _en(message)))
        for p, v in ((SH.minCount, min_count), (SH.maxCount, max_count)):
            if v is not None:
                g.add((s, p, Literal(v)))
        for p, v in ((SH["class"], cls), (SH.datatype, datatype)):
            if v is not None:
                g.add((s, p, v))
        if pattern is not None:
            g.add((s, SH.pattern, Literal(pattern)))

    def sparql(owner: URIRef, name: str, message: str, select: str) -> None:
        s = ARS[name]
        g.add((owner, SH.sparql, s))
        g.add((s, RDF.type, SH.SPARQLConstraint))
        g.add((s, SH.message, _en(message)))
        g.add((s, SH.select, Literal(select)))

    be = node_shape(ROOT_CLASS, AR[ROOT_CLASS], "Every business entity (every value in the graph).")
    prop_shape(be, "BusinessEntity-prefLabel", SKOS.prefLabel, "Exactly one English label.",
               min_count=1, max_count=1, datatype=RDF.langString)
    prop_shape(be, "BusinessEntity-column", AR.column, "Exactly one warehouse column.",
               min_count=1, max_count=1, datatype=XSD.string)
    prop_shape(be, "BusinessEntity-inScheme", SKOS.inScheme, "In a concept scheme.",
               min_count=1, cls=SKOS.ConceptScheme)
    sparql(be, "UniqueLabelPerClass", "Two values of one class share a label.", f"""
SELECT $this ?value WHERE {{
  $this <{SKOS.prefLabel}> ?value ; a ?cls .
  ?cls <{RDFS.subClassOf}>+ <{AR[ROOT_CLASS]}> .
  ?other <{SKOS.prefLabel}> ?value ; a ?cls .
  FILTER (?other != $this)
}}""")
    sparql(be, "BroaderAcyclic", "The hierarchy has a cycle through this value.",
           f"SELECT $this WHERE {{ $this <{SKOS.broader}>+ $this }}")

    by_class: dict[str, list[Relation]] = {}
    for r in spec.relations:
        by_class.setdefault(spec.column_class[r.child], []).append(r)
    for cname, rels in by_class.items():
        ns = node_shape(cname, AR[cname], f"Every {spec.classes[cname]['label']}.")
        for r in rels:
            to = spec.column_class[r.to]
            label = spec.classes[to]["label"]
            if r.hierarchical:
                msg = (f"Exactly one {label}." if r.required else f"At most one {label}.")
            else:
                msg = f"{r.prop}: a {label}" + (", at most one." if r.functional else ".")
            prop_shape(ns, f"{cname}-{r.prop}", AR[r.prop], msg,
                       min_count=1 if r.required else None,
                       max_count=1 if (r.functional or r.hierarchical) else None, cls=AR[to])

    ms = node_shape("Metric", AR.Metric, "Every governed metric.")
    for name, path, dt in (("prefLabel", SKOS.prefLabel, RDF.langString),
                           ("definition", SKOS.definition, RDF.langString),
                           ("metricName", AR.metricName, XSD.string),
                           ("expression", AR.expression, XSD.string),
                           ("owner", AR.owner, XSD.string),
                           ("table", AR.table, XSD.string)):
        prop_shape(ms, f"Metric-{name}", path, f"Exactly one {name}.", min_count=1, max_count=1, datatype=dt)
    prop_shape(ms, "Metric-inScheme", SKOS.inScheme, "In a concept scheme.", min_count=1,
               cls=SKOS.ConceptScheme)
    prop_shape(ms, "Metric-derivesFrom", AR.derivesFrom, "Derives only from governed metrics.",
               cls=AR.Metric)
    sparql(ms, "PrefAltDisjoint", "A synonym equals the preferred label (SKOS S13).", f"""
SELECT $this ?value WHERE {{ $this <{SKOS.prefLabel}> ?value ; <{SKOS.altLabel}> ?value }}""")

    gs = node_shape("MetricGroup", AR.MetricGroup, "Every ambiguous metric word.")
    prop_shape(gs, "MetricGroup-prefLabel", SKOS.prefLabel, "Exactly one label.", min_count=1,
               max_count=1, datatype=RDF.langString)
    prop_shape(gs, "MetricGroup-narrower", SKOS.narrower, "Names at least two governed metrics.",
               min_count=2, cls=AR.Metric)

    cs = node_shape("Chunk", AR.Chunk, "Every document chunk.")
    prop_shape(cs, "Chunk-label", RDFS.label, "Exactly one title.", min_count=1, max_count=1,
               datatype=XSD.string)
    prop_shape(cs, "Chunk-source", AR.source, "Exactly one source.", min_count=1, max_count=1,
               datatype=XSD.string)
    prop_shape(cs, "Chunk-contentHash", AR.contentHash, "Exactly one SHA-256 content hash.",
               min_count=1, max_count=1, datatype=XSD.string, pattern=HASH_PATTERN)
    prop_shape(cs, "Chunk-mentions", AR.mentions, "Every entity tag resolves to a value in the graph.",
               cls=AR[ROOT_CLASS])
    return g


# --- ABox (instances) --------------------------------------------------------------------

def _pairs(conn: Any, idx: Any, child: str, parent: str) -> list[tuple[str, str]]:
    """Value pairs of a declared link: the value index's foreign-key pairs, or the same row."""
    if (child, parent) in idx.pairs:
        return idx.pairs[(child, parent)]
    ct, cc = child.split(".", 1)
    pt, pc = parent.split(".", 1)
    if ct != pt:
        raise ValueError(f"no foreign key from {ct} to {pt} for {child} → {parent}")
    rows = conn.execute(
        f"SELECT DISTINCT {_q(cc)}, {_q(pc)} FROM {_q(ct)} WHERE {_q(cc)} IS NOT NULL "
        f"AND {_q(pc)} IS NOT NULL ORDER BY 1, 2 LIMIT {MAX_PAIRS}").fetchall()
    return [(str(a), str(b)) for a, b in rows]


def build_instances(db: str | Path, entities_path: str | Path = DEFAULT_ENTITIES,
                    documents_path: str | Path = DEFAULT_DOCUMENTS,
                    metrics_path: str | Path = DEFAULT_METRICS) -> Graph:
    """The ABox of one database: values, their hierarchy and relations, document chunks."""
    spec = load_spec(entities_path)
    sg = build_schema_graph(db, metrics_path)
    idx = build_value_index(db, sg, entities_path)
    g = _graph()
    no_parent = {spec.column_class[c] for c in spec.column_class} - {
        spec.column_class[r.child] for r in spec.relations if r.hierarchical}
    for col, vals in idx.values.items():
        cname = spec.column_class[col]
        scheme = scheme_iri(spec.scheme_of(cname))
        for v in vals:
            node = entity_iri(cname, v)
            g.add((node, RDF.type, AR[cname]))
            g.add((node, SKOS.prefLabel, _en(v)))
            g.add((node, AR.column, Literal(col)))
            g.add((node, SKOS.inScheme, scheme))
            if cname in no_parent:
                g.add((node, SKOS.topConceptOf, scheme))
            key = norm(v)
            if _matchable(key):
                g.add((node, SKOS.hiddenLabel, _en(" ".join(key))))
    conn = connect_ro(db)
    try:
        for r in spec.relations:
            if r.child not in idx.values or r.to not in idx.values:
                continue
            ca, cb = spec.column_class[r.child], spec.column_class[r.to]
            for a, b in _pairs(conn, idx, r.child, r.to):
                na, nb = entity_iri(ca, a), entity_iri(cb, b)
                g.add((na, AR[r.prop], nb))
                if r.hierarchical:
                    g.add((na, SKOS.broader, nb))
                    g.add((nb, SKOS.narrower, na))
    finally:
        conn.close()
    chunks, _ = load_chunks(db, documents_path)
    for c in chunks:
        node = chunk_iri(c.id)
        g.add((node, RDF.type, AR.Chunk))
        g.add((node, RDFS.label, Literal(c.title)))
        g.add((node, AR.source, Literal(c.source)))
        g.add((node, AR.contentHash, Literal(c.hash)))
        for col, v in c.entities.items():
            cname = spec.column_class.get(col)
            g.add((node, AR.mentions, entity_iri(cname, v) if cname else URIRef(
                f"{ID}unknown/{quote(col, safe='')}/{quote(v, safe='')}")))
    return g


def full_graph(db: str | Path | None = None, entities_path: str | Path = DEFAULT_ENTITIES,
               metrics_path: str | Path = DEFAULT_METRICS,
               documents_path: str | Path = DEFAULT_DOCUMENTS) -> Graph:
    """TBox + vocabulary (+ the instances of ``db``): the graph SHACL validates and SPARQL reads."""
    g = _graph()
    for part in (build_tbox(entities_path), build_vocabulary(entities_path, metrics_path)):
        g += part
    if db is not None:
        g += build_instances(db, entities_path, documents_path, metrics_path)
    return g


# --- validation and queries --------------------------------------------------------------

@dataclass
class Validation:
    conforms: bool
    results: Graph
    text: str

    def violations(self) -> list[dict[str, Any]]:
        out = []
        for r in self.results.subjects(RDF.type, SH.ValidationResult):
            out.append({k: self.results.value(r, p) for k, p in (
                ("focus", SH.focusNode), ("shape", SH.sourceShape),
                ("component", SH.sourceConstraintComponent), ("constraint", SH.sourceConstraint),
                ("message", SH.resultMessage))})
        return out


def validate(data: Graph, shapes: Graph | None = None) -> Validation:
    import pyshacl

    conforms, results, text = pyshacl.validate(data, shacl_graph=shapes if shapes is not None
                                               else build_shapes(), inference="none")
    return Validation(bool(conforms), results, text)


def _rows(g: Graph, query: str, **bind: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in g.query(query, initBindings=bind)]


ROLLUP = f"""
SELECT DISTINCT ?label WHERE {{
  ?x a ?cls ; <{SKOS.prefLabel}> ?label ; <{SKOS.broader}>+ ?anc .
  ?anc a ?ancCls ; <{SKOS.prefLabel}> ?ancLabel .
}}"""


def members(g: Graph, cls: str, ancestor_cls: str, ancestor: str) -> set[str]:
    """Values of ``cls`` that roll up (``skos:broader``, any depth) to ``ancestor``."""
    return {str(r[0]) for r in _rows(g, ROLLUP, cls=AR[cls], ancCls=AR[ancestor_cls],
                                     ancLabel=_en(ancestor))}


COUNT_BY_PARENT = f"""
SELECT ?parentLabel (COUNT(DISTINCT ?x) AS ?n) WHERE {{
  ?x a ?cls ; ?prop ?parent . ?parent <{SKOS.prefLabel}> ?parentLabel .
}} GROUP BY ?parentLabel"""


def count_by_parent(g: Graph, cls: str, prop: str) -> dict[str, int]:
    return {str(p): int(n) for p, n in _rows(g, COUNT_BY_PARENT, cls=AR[cls], prop=AR[prop])}


RELATED = f"""
SELECT DISTINCT ?label WHERE {{
  ?x a ?cls ; <{SKOS.prefLabel}> ?label ; ?prop ?place .
  ?place <{SKOS.broader}>* ?anc . ?anc a ?ancCls ; <{SKOS.prefLabel}> ?ancLabel .
}}"""


def related_under(g: Graph, cls: str, prop: str, ancestor_cls: str, ancestor: str) -> set[str]:
    """Values of ``cls`` whose ``prop`` target is ``ancestor`` or rolls up to it."""
    return {str(r[0]) for r in _rows(g, RELATED, cls=AR[cls], prop=AR[prop], ancCls=AR[ancestor_cls],
                                     ancLabel=_en(ancestor))}


RESOLVE = f"""
SELECT ?column ?value WHERE {{ ?x <{SKOS.hiddenLabel}> ?key ; <{AR.column}> ?column ;
                                  <{SKOS.prefLabel}> ?value . }}"""


def resolve_name(g: Graph, phrase: str) -> set[tuple[str, str]]:
    """(column, exact value) for every value whose match key equals the phrase's."""
    return {(str(c), str(v)) for c, v in _rows(g, RESOLVE, key=_en(" ".join(norm(phrase))))}


AMBIGUOUS = f"""
SELECT ?key WHERE {{ ?x <{SKOS.hiddenLabel}> ?key ; <{AR.column}> ?column . }}
GROUP BY ?key HAVING (COUNT(DISTINCT ?column) > 1)"""


def ambiguous_names(g: Graph) -> set[str]:
    """Match keys that are a value of two or more columns ("canada": a region and a country)."""
    return {str(r[0]) for r in _rows(g, AMBIGUOUS)}


RESOLVE_METRIC = f"""
SELECT DISTINCT ?name WHERE {{
  {{ ?m a <{AR.Metric}> ; <{SKOS.altLabel}>|<{SKOS.prefLabel}>|<{AR.metricName}> ?l . }}
  UNION
  {{ ?grp a <{AR.MetricGroup}> ; <{SKOS.altLabel}>|<{SKOS.prefLabel}> ?l ; <{SKOS.narrower}> ?m . }}
  ?m <{AR.metricName}> ?name .
  FILTER (lcase(str(?l)) = ?phrase)
}}"""


def resolve_metric(g: Graph, phrase: str) -> set[str]:
    """Metric names a word names: directly (label, synonym) or through an ambiguous group."""
    return {str(r[0]) for r in _rows(g, RESOLVE_METRIC, phrase=Literal(phrase.lower()))}


CHUNKS_ABOUT = f"""
SELECT DISTINCT ?c WHERE {{ ?c a <{AR.Chunk}> ; <{AR.mentions}> ?e . }}"""


def chunks_about(g: Graph, cls: str, value: str) -> set[str]:
    """IRIs of the chunks tagged with this value."""
    return {str(r[0]) for r in _rows(g, CHUNKS_ABOUT, e=entity_iri(cls, value))}


# --- files and CLI -----------------------------------------------------------------------

def generated(entities_path: str | Path = DEFAULT_ENTITIES,
              metrics_path: str | Path = DEFAULT_METRICS) -> dict[str, Graph]:
    return {"tbox": build_tbox(entities_path),
            "vocabulary": build_vocabulary(entities_path, metrics_path),
            "shapes": build_shapes(entities_path)}


def write_ttl(g: Graph, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(g.serialize(format="turtle"))
    return path


def read_ttl(path: str | Path) -> Graph:
    return Graph().parse(Path(path), format="turtle")


def stale(out: str | Path = DEFAULT_OUT, **paths: Any) -> list[str]:
    """Committed files that differ from what the specs generate (no blank nodes: set equality)."""
    bad = []
    for key, g in generated(**paths).items():
        p = Path(out) / FILES[key]
        if not p.is_file() or set(read_ttl(p)) != set(g):
            bad.append(str(p))
    return bad


def stats(g: Graph) -> dict[str, int]:
    def n(q: str) -> int:
        return int(next(iter(g.query(q)))[0])

    return {
        "triples": len(g),
        "classes": len(set(g.subjects(RDF.type, OWL.Class))),
        "object_properties": len(set(g.subjects(RDF.type, OWL.ObjectProperty))),
        "datatype_properties": len(set(g.subjects(RDF.type, OWL.DatatypeProperty))),
        "concept_schemes": len(set(g.subjects(RDF.type, SKOS.ConceptScheme))),
        "concepts": n(f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?x a/<{RDFS.subClassOf}>* "
                      f"<{SKOS.Concept}> . FILTER NOT EXISTS {{ ?x a <{OWL.Class}> }} }}"),
        "broader_links": len(set(g.subject_objects(SKOS.broader))),
        "alt_labels": len(set(g.subject_objects(SKOS.altLabel))),
        "chunks": len(set(g.subjects(RDF.type, AR.Chunk))),
        "node_shapes": len(set(g.subjects(RDF.type, SH.NodeShape))),
        "property_shapes": len(set(g.subjects(RDF.type, SH.PropertyShape))),
        "sparql_constraints": len(set(g.subjects(RDF.type, SH.SPARQLConstraint))),
    }


def _fmt(s: dict[str, int]) -> str:
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in s.items() if v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the ontology (OWL, SKOS, SHACL) from the specs; "
                                             "optionally build and validate one database's instances.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder of the committed .ttl files")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if the files are stale")
    ap.add_argument("--db", help="also build this database's instance graph and validate it with SHACL")
    ap.add_argument("--instances", default=None, help="where to write the instance graph (.ttl)")
    args = ap.parse_args(argv)
    if args.check:
        bad = stale(args.out)
        if bad:
            print("stale (run python -m agentic_reporting.ontology): " + ", ".join(bad))
            return 1
        print(f"{args.out}: up to date with specs/")
    else:
        for key, g in generated().items():
            path = write_ttl(g, Path(args.out) / FILES[key])
            print(f"wrote {path}: {_fmt(stats(g))}")
    if args.db:
        abox = build_instances(args.db)
        out = Path(args.instances or DEFAULT_INSTANCES / f"{Path(args.db).stem}.ttl")
        write_ttl(abox, out)
        print(f"wrote {out}: {_fmt(stats(abox))}")
        data = full_graph(args.db)
        v = validate(data)
        print(f"SHACL over {len(data)} triples: " + ("conforms" if v.conforms else
                                                      f"{len(v.violations())} violations"))
        if not v.conforms:
            print(v.text)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
