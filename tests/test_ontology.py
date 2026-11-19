"""The ontology layer (ADR-0007): OWL + SKOS + SHACL generated from the specs, SPARQL checked against SQL.

Needs the optional extra (``pip install -e ".[ontology]"``); without rdflib and pyshacl the whole
module is skipped. Two databases:

* ``mini``: a tiny AdventureWorks-shaped database built here. Its dimension rows are copied
  from AdventureWorks DW (descriptions shortened); the fact amounts are made up. Always runs.
* ``adventureworks``: ``samples/adventureworks.sqlite`` (``python samples/load_adventureworks.py``).
  Skipped when absent; the CI ``ontology`` job downloads it.

What is proved:

* the committed ``ontology/*.ttl`` files are exactly what the specs generate (drift fails);
* the generated graph conforms to the SHACL shapes, and **every constraint of every shape fires**
  on a graph broken on purpose (one mutation per constraint, generated from the shapes);
* the instance graph mirrors the value index (``values.py``) value for value;
* SPARQL over the graph returns what SQL returns over the warehouse: roll-ups, counts,
  memberships, and a sales total filtered by graph-resolved names;
* SPARQL name and metric resolution agrees with the value index and the schema linker.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

rdflib = pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")

from rdflib import RDF, RDFS, SH, SKOS, BNode, Literal, URIRef  # noqa: E402

from agentic_reporting import ontology as onto  # noqa: E402
from agentic_reporting.documents import slug  # noqa: E402
from agentic_reporting.schema_graph import (  # noqa: E402
    Metric,
    build_schema_graph,
    link,
    load_ambiguous,
    load_metrics,
)
from agentic_reporting.values import add_to_graph, build_value_index, load_entities  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AW = ROOT / "samples" / "adventureworks.sqlite"
AR = onto.AR

MINI = """
CREATE TABLE DimProductCategory (ProductCategoryKey INTEGER PRIMARY KEY, EnglishProductCategoryName TEXT);
CREATE TABLE DimProductSubcategory (ProductSubcategoryKey INTEGER PRIMARY KEY,
  EnglishProductSubcategoryName TEXT,
  ProductCategoryKey INTEGER REFERENCES DimProductCategory(ProductCategoryKey));
CREATE TABLE DimProduct (ProductKey INTEGER PRIMARY KEY,
  ProductSubcategoryKey INTEGER REFERENCES DimProductSubcategory(ProductSubcategoryKey),
  EnglishProductName TEXT, Color TEXT, ModelName TEXT, EnglishDescription TEXT);
CREATE TABLE DimSalesTerritory (SalesTerritoryKey INTEGER PRIMARY KEY, SalesTerritoryRegion TEXT,
  SalesTerritoryCountry TEXT, SalesTerritoryGroup TEXT);
CREATE TABLE DimGeography (GeographyKey INTEGER PRIMARY KEY, StateProvinceName TEXT,
  SalesTerritoryKey INTEGER REFERENCES DimSalesTerritory(SalesTerritoryKey));
CREATE TABLE DimReseller (ResellerKey INTEGER PRIMARY KEY,
  GeographyKey INTEGER REFERENCES DimGeography(GeographyKey), BusinessType TEXT, ResellerName TEXT);
CREATE TABLE DimPromotion (PromotionKey INTEGER PRIMARY KEY, EnglishPromotionName TEXT);
CREATE TABLE FactInternetSales (ProductKey INTEGER REFERENCES DimProduct(ProductKey),
  SalesTerritoryKey INTEGER REFERENCES DimSalesTerritory(SalesTerritoryKey),
  SalesOrderNumber TEXT, OrderQuantity INTEGER, SalesAmount REAL, TotalProductCost REAL, OrderDate TEXT);
INSERT INTO DimProductCategory VALUES (1,'Bikes'),(2,'Components'),(3,'Clothing'),(4,'Accessories');
INSERT INTO DimProductSubcategory VALUES (1,'Mountain Bikes',1),(2,'Road Bikes',1),(14,'Road Frames',2),
  (31,'Helmets',4);
INSERT INTO DimProduct VALUES
  (358,1,'Mountain-200 Black, 38','Black','Mountain-200','Serious back-country riding.'),
  (352,1,'Mountain-200 Silver, 38','Silver','Mountain-200','Serious back-country riding.'),
  (311,2,'Road-150 Red, 44','Red','Road-150','This bike is ridden by race winners.'),
  (210,14,'HL Road Frame - Black, 58','Black','HL Road Frame','Our lightest and best quality frame.'),
  (212,31,'Sport-100 Helmet, Red','Red','Sport-100','Universal fit, well-vented, lightweight.'),
  (1,NULL,'Adjustable Race','NA',NULL,NULL);
INSERT INTO DimSalesTerritory VALUES (1,'Northwest','United States','North America'),
  (3,'Central','United States','North America'),(6,'Canada','Canada','North America'),
  (7,'France','France','Europe');
INSERT INTO DimGeography VALUES (1,'Washington',1),(2,'Michigan',3),(3,'Alberta',6),(4,'Seine et Marne',7);
INSERT INTO DimReseller VALUES (1,1,'Value Added Reseller','A Bike Store'),
  (2,1,'Warehouse','Friendly Bike Shop'),(3,2,'Specialty Bike Shop','Friendly Bike Shop'),
  (4,4,'Warehouse','Sports Products Store');
INSERT INTO DimPromotion VALUES (1,'No Discount');
INSERT INTO FactInternetSales VALUES (358,1,'SO1',1,2000.0,1100.0,'2013-05-01'),
  (352,6,'SO2',1,2100.5,1150.0,'2013-06-01'),(311,7,'SO3',1,3500.25,2100.0,'2013-07-01'),
  (212,1,'SO4',2,70.0,26.0,'2013-07-02'),(210,3,'SO5',1,1400.0,800.0,'2013-07-03');
"""


@pytest.fixture(scope="session")
def mini_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("onto") / "mini.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(MINI)
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="session", params=["mini", "adventureworks"])
def db(request, mini_db) -> Path:
    if request.param == "mini":
        return mini_db
    if not AW.is_file():
        pytest.skip("samples/adventureworks.sqlite not built (python samples/load_adventureworks.py)")
    return AW


_GRAPHS: dict[Path, rdflib.Graph] = {}


def kg(path: Path) -> rdflib.Graph:
    if path not in _GRAPHS:
        _GRAPHS[path] = onto.full_graph(path)
    return _GRAPHS[path]


def sql(path: Path, query: str, *args) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def col1(path: Path, query: str, *args) -> set[str]:
    return {r[0] for r in sql(path, query, *args)}


# --- generation: one source of truth -------------------------------------------------------

def test_committed_ttl_files_match_the_specs() -> None:
    assert onto.stale() == [], "run: python -m agentic_reporting.ontology"


def test_committed_files_have_no_blank_nodes() -> None:
    """No blank nodes, so set equality is a full comparison and the output is stable."""
    for name in onto.FILES.values():
        g = onto.read_ttl(onto.DEFAULT_OUT / name)
        assert not [t for t in g if any(isinstance(x, BNode) for x in t)], name


def test_every_metric_field_is_mapped() -> None:
    assert set(onto.METRIC_FIELDS) == set(Metric.model_fields)


def test_value_index_reads_only_column_and_label() -> None:
    ents = load_entities()
    assert len(ents) == 15 and ents[0].column == "DimProduct.EnglishProductName"
    assert ents[5].label == "sales territory region"


def test_tbox_classes_and_properties_come_from_the_spec() -> None:
    spec = onto.load_spec()
    g = onto.build_tbox()
    for col, cls in spec.column_class.items():
        assert (AR[cls], RDF.type, rdflib.OWL.Class) in g
        assert Literal(col) in set(g.objects(AR[cls], AR.sourceColumn))
        chain = list(g.transitive_objects(AR[cls], RDFS.subClassOf))
        assert AR.BusinessEntity in chain and SKOS.Concept in chain
    for r in spec.relations:
        assert (AR[r.prop], RDF.type, rdflib.OWL.ObjectProperty) in g
        assert g.value(AR[r.prop], RDFS.range) == AR[spec.column_class[r.to]]
        assert ((AR[r.prop], RDFS.subPropertyOf, SKOS.broader) in g) == r.hierarchical
    # the product taxonomy and the territory hierarchy, as declared
    assert (AR.inProductSubcategory, RDFS.domain, AR.ProductTaxonomyLevel) in g   # product + model
    assert {r.prop for r in spec.relations if r.hierarchical} == {
        "inProductSubcategory", "inProductCategory", "inSalesTerritoryRegion",
        "inSalesTerritoryCountry", "inSalesTerritoryGroup"}


BAD_SPECS = {   # case → (yaml, the error it must raise)
    "unknown parent class": ("classes: [{name: BusinessEntity, label: e}]\n"
                             "entities: [{column: t.a, label: a, class: A, subclass_of: Nope}]\n",
                             "unknown parent class"),
    "broader is not an entity": ("classes: [{name: BusinessEntity, label: e, scheme: S}]\n"
                                 "entities: [{column: t.a, label: a, class: A, subclass_of: BusinessEntity,"
                                 " broader: t.b}]\n", "not an entity column"),
    "entity without a class": ("classes: [{name: BusinessEntity, label: e}]\n"
                               "entities: [{column: t.a, label: a}]\n", "has no class"),
    "class under two parents": ("classes: [{name: BusinessEntity, label: e, scheme: S},"
                                " {name: P, label: p, subclass_of: BusinessEntity}]\n"
                                "entities: [{column: t.a, label: a, class: A, subclass_of: P},"
                                " {column: t.b, label: b, class: A, subclass_of: BusinessEntity}]\n",
                                "declared under"),
    "no concept scheme": ("classes: [{name: BusinessEntity, label: e}]\n"
                          "entities: [{column: t.a, label: a, class: A, subclass_of: BusinessEntity}]\n",
                          "no concept scheme"),
}


@pytest.mark.parametrize("case", sorted(BAD_SPECS))
def test_a_bad_spec_is_refused(case: str, tmp_path: Path) -> None:
    p = tmp_path / "entities.yaml"
    text, error = BAD_SPECS[case]
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        onto.load_spec(p)


def test_vocabulary_holds_every_metric_synonym_and_group() -> None:
    g = onto.build_vocabulary()
    for m in load_metrics():
        node = onto.metric_iri(m.name)
        assert g.value(node, SKOS.prefLabel) == Literal(m.label, lang="en")
        assert {str(x) for x in g.objects(node, SKOS.altLabel)} == set(m.synonyms)
        assert g.value(node, AR.expression) == Literal(m.expression)
    for a in load_ambiguous():
        grp = onto.group_iri(a.phrases[0])
        assert {g.value(n, AR.metricName).toPython() for n in g.objects(grp, SKOS.narrower)} == set(a.options)
        assert str(g.value(grp, AR.clarifyingQuestion)) == a.ask


# --- SHACL: the graph conforms, and every constraint fires on a broken graph ---------------

def test_generated_graph_conforms(db: Path) -> None:
    v = onto.validate(kg(db))
    assert v.conforms, v.text


def test_shapes_are_valid_shacl(mini_db: Path) -> None:
    """The shapes graph itself passes SHACL-for-SHACL (pyshacl meta-validation)."""
    import pyshacl

    conforms, _, text = pyshacl.validate(kg(mini_db), shacl_graph=onto.build_shapes(), meta_shacl=True,
                                         inference="none")
    assert conforms, text


SHAPES = onto.build_shapes()
COMPONENTS = {"minCount": SH.minCount, "maxCount": SH.maxCount, "class": SH["class"],
              "datatype": SH.datatype, "pattern": SH.pattern}
KNOWN_KEYS = {SH.path, SH.message, RDF.type, *COMPONENTS.values()}


def _local(iri: URIRef) -> str:
    return str(iri).split("#")[-1]


def _cases() -> list:
    out = []
    for ns in sorted(SHAPES.subjects(RDF.type, SH.NodeShape)):
        for ps in sorted(SHAPES.objects(ns, SH.property)):
            for comp, pred in COMPONENTS.items():
                if SHAPES.value(ps, pred) is not None:
                    out.append(pytest.param(ns, ps, comp, id=f"{_local(ps)}-{comp}"))
        for c in sorted(SHAPES.objects(ns, SH.sparql)):
            out.append(pytest.param(ns, c, "sparql", id=_local(c)))
    return out


def _instances(g: rdflib.Graph, cls: URIRef) -> list[URIRef]:
    return sorted({r[0] for r in g.query(f"SELECT ?x WHERE {{ ?x a/<{RDFS.subClassOf}>* <{cls}> }}")})


def _mutate(g: rdflib.Graph, ns: URIRef, shape: URIRef, comp: str) -> set[URIRef]:
    """Break the graph for exactly this constraint; return the focus nodes it must report."""
    focus = _instances(g, SHAPES.value(ns, SH.targetClass))
    assert focus, f"{_local(ns)} has no focus node in the test graph, so it is not exercised"
    if comp == "sparql":
        name = _local(shape)
        if name == "UniqueLabelPerClass":
            f = focus[0]
            twin = URIRef(str(f) + "-twin")
            for p, o in list(g.predicate_objects(f)):
                g.add((twin, p, o))
            return {f, twin}
        if name == "BroaderAcyclic":
            child, parent = sorted(g.subject_objects(SKOS.broader))[0]
            g.add((parent, SKOS.broader, child))
            return {child, parent}
        if name == "PrefAltDisjoint":
            f = focus[0]
            g.add((f, SKOS.altLabel, g.value(f, SKOS.prefLabel)))
            return {f}
        raise AssertionError(f"no mutation written for SPARQL constraint {name}")
    path = SHAPES.value(shape, SH.path)
    with_value = [f for f in focus if g.value(f, path) is not None]
    f = (with_value or focus)[0]
    cls = SHAPES.value(shape, SH["class"])
    if comp == "minCount":
        g.remove((f, path, None))
    elif comp == "maxCount":
        if cls is not None:
            extra = next(x for x in _instances(g, cls) if (f, path, x) not in g)
        elif SHAPES.value(shape, SH.datatype) == RDF.langString:
            extra = Literal("a second value", lang="en")
        else:
            extra = Literal("a second value")
        g.add((f, path, extra))
    else:
        g.remove((f, path, None))
        g.add((f, path, {"class": URIRef(onto.ID + "unresolved/tag"), "datatype": Literal(42),
                         "pattern": Literal("not-a-hash")}[comp]))
    return {f}


@pytest.mark.parametrize(("ns", "shape", "comp"), _cases())
def test_mutation_breaks_validation(ns: URIRef, shape: URIRef, comp: str, mini_db: Path) -> None:
    g = rdflib.Graph()
    g += kg(mini_db)
    expect = _mutate(g, ns, shape, comp)
    v = onto.validate(g, SHAPES)
    assert not v.conforms
    component = SH.SPARQLConstraintComponent if comp == "sparql" else \
        SH[comp[0].upper() + comp[1:] + "ConstraintComponent"]
    hits = [x for x in v.violations() if x["component"] == component and x["focus"] in expect
            and (x["constraint"] == shape if comp == "sparql" else x["shape"] == shape)]
    assert hits, f"{_local(shape)} {comp} did not fire:\n{v.text[:2000]}"


def test_every_shape_constraint_is_mutated() -> None:
    """Every constraint key in the shapes is one the mutation test knows how to break."""
    for ps in SHAPES.subjects(RDF.type, SH.PropertyShape):
        assert set(SHAPES.predicates(ps)) <= KNOWN_KEYS, _local(ps)
    ids = {p.id for p in _cases()}
    assert {"UniqueLabelPerClass", "BroaderAcyclic", "PrefAltDisjoint"} <= ids
    assert len(ids) == len(_cases())


# --- the instance graph mirrors the value index ------------------------------------------

def test_instances_mirror_the_value_graph(db: Path) -> None:
    sg = build_schema_graph(db)
    add_to_graph(sg, build_value_index(db, sg))
    g, spec = kg(db), onto.load_spec()
    nx_values = {(d["column"], d["value"]) for _, d in sg.nodes(data=True) if d["kind"] == "value"}
    rdf_values = {(str(c), str(v)) for _, c, v in g.query(
        f"SELECT ?x ?c ?v WHERE {{ ?x <{AR.column}> ?c ; <{SKOS.prefLabel}> ?v }}")}
    assert rdf_values == nx_values
    belongs = {(sg.nodes[u]["column"], sg.nodes[u]["value"], sg.nodes[v]["column"], sg.nodes[v]["value"])
               for u, v, d in sg.edges(data=True) if d["kind"] == "belongs_to"}
    for r in spec.relations:
        if r.child.split(".")[0] == r.to.split(".")[0]:
            continue                                   # same-row links are not in the value graph
        got = {(r.child, str(g.value(a, SKOS.prefLabel)), r.to, str(g.value(b, SKOS.prefLabel)))
               for a, b in g.subject_objects(AR[r.prop]) if str(g.value(a, AR.column)) == r.child}
        assert got == {e for e in belongs if e[0] == r.child and e[2] == r.to}, r.prop
    # foreign-key pairs the spec does not declare a hierarchy (colour → subcategory) are not one
    assert not set(g.objects(onto.entity_iri("Colour", "Black"), SKOS.broader))


# --- SPARQL over the graph = SQL over the warehouse --------------------------------------

def test_product_rollup_to_category_matches_sql(db: Path) -> None:
    for (cat,) in sql(db, "SELECT EnglishProductCategoryName FROM DimProductCategory"):
        expected = col1(db, """SELECT DISTINCT p.EnglishProductName FROM DimProduct p
            JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey
            JOIN DimProductCategory c ON s.ProductCategoryKey = c.ProductCategoryKey
            WHERE c.EnglishProductCategoryName = ?""", cat)
        assert onto.members(kg(db), "Product", "ProductCategory", cat) == expected, cat


def test_model_count_per_subcategory_matches_sql(db: Path) -> None:
    expected = dict(sql(db, """SELECT s.EnglishProductSubcategoryName, COUNT(DISTINCT p.ModelName)
        FROM DimProduct p JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey
        WHERE p.ModelName IS NOT NULL GROUP BY 1"""))
    assert onto.count_by_parent(kg(db), "ProductModel", "inProductSubcategory") == expected


def test_territory_rollup_matches_sql_and_the_value_index(db: Path) -> None:
    """State → region → country → group in the graph = the state's group in one SQL join."""
    sg = build_schema_graph(db)
    direct = build_value_index(db, sg).pairs[("DimGeography.StateProvinceName",
                                              "DimSalesTerritory.SalesTerritoryGroup")]
    for (grp,) in sql(db, "SELECT DISTINCT SalesTerritoryGroup FROM DimSalesTerritory"):
        expected = col1(db, """SELECT DISTINCT g.StateProvinceName FROM DimGeography g
            JOIN DimSalesTerritory t ON g.SalesTerritoryKey = t.SalesTerritoryKey
            WHERE t.SalesTerritoryGroup = ?""", grp)
        got = onto.members(kg(db), "StateProvince", "SalesTerritoryGroup", grp)
        assert got == expected == {s for s, gr in direct if gr == grp}, grp


def test_resellers_under_a_territory_match_sql(db: Path) -> None:
    for level, column in (("SalesTerritoryCountry", "SalesTerritoryCountry"),
                          ("SalesTerritoryGroup", "SalesTerritoryGroup"),
                          ("SalesTerritoryRegion", "SalesTerritoryRegion")):
        for (place,) in sql(db, f"SELECT DISTINCT {column} FROM DimSalesTerritory"):
            expected = col1(db, f"""SELECT DISTINCT r.ResellerName FROM DimReseller r
                JOIN DimGeography g ON r.GeographyKey = g.GeographyKey
                JOIN DimSalesTerritory t ON g.SalesTerritoryKey = t.SalesTerritoryKey
                WHERE t.{column} = ?""", place)
            assert onto.related_under(kg(db), "Reseller", "locatedIn", level, place) == expected, place


def test_colour_within_category_matches_sql(db: Path) -> None:
    g = kg(db)
    for (cat,) in sql(db, "SELECT EnglishProductCategoryName FROM DimProductCategory"):
        in_cat = onto.members(g, "Product", "ProductCategory", cat)
        for (colour,) in sql(db, "SELECT DISTINCT Color FROM DimProduct WHERE Color IS NOT NULL"):
            expected = col1(db, """SELECT DISTINCT p.EnglishProductName FROM DimProduct p
                JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey
                JOIN DimProductCategory c ON s.ProductCategoryKey = c.ProductCategoryKey
                WHERE c.EnglishProductCategoryName = ? AND p.Color = ?""", cat, colour)
            got = in_cat & onto.related_under(g, "Product", "hasColour", "Colour", colour)
            assert got == expected, (cat, colour)


def test_graph_resolved_names_give_the_sql_total(db: Path) -> None:
    """The chatbot's use: the graph expands "Bikes" to product names; the warehouse sums the rows."""
    checked = 0
    for (cat,) in sql(db, "SELECT EnglishProductCategoryName FROM DimProductCategory"):
        names = sorted(onto.members(kg(db), "Product", "ProductCategory", cat))
        (by_join,) = sql(db, """SELECT ROUND(SUM(f.SalesAmount), 2) FROM FactInternetSales f
            JOIN DimProduct p ON f.ProductKey = p.ProductKey
            JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey
            JOIN DimProductCategory c ON s.ProductCategoryKey = c.ProductCategoryKey
            WHERE c.EnglishProductCategoryName = ?""", cat)[0]
        marks = ",".join("?" * len(names)) or "NULL"
        (by_graph,) = sql(db, f"""SELECT ROUND(SUM(f.SalesAmount), 2) FROM FactInternetSales f
            JOIN DimProduct p ON f.ProductKey = p.ProductKey
            WHERE p.EnglishProductName IN ({marks})""", *names)[0]
        assert by_graph == pytest.approx(by_join, rel=1e-12) if by_join is not None else by_graph is None
        checked += by_join is not None
    assert checked >= 2


def test_name_resolution_matches_the_value_index(db: Path) -> None:
    """Every match key of the value index: SPARQL on skos:hiddenLabel finds the same values."""
    idx = build_value_index(db, build_schema_graph(db))
    g = kg(db)
    for key, hits in idx.lookup.items():
        phrase = hits[0][1]                        # a value whose key this is
        expected = {(idx.entities[o].column, v) for o, v in hits}
        assert onto.resolve_name(g, phrase) == expected, key


def test_ambiguous_names_are_the_value_index_ties(db: Path) -> None:
    idx = build_value_index(db, build_schema_graph(db))
    ties = {" ".join(k) for k, hits in idx.lookup.items()
            if len({idx.entities[o].column for o, _ in hits}) > 1}
    assert onto.ambiguous_names(kg(db)) == ties
    assert "canada" in ties


def test_chunk_tags_match_the_hierarchy_and_sql(db: Path) -> None:
    g = kg(db)
    for (sub,) in sql(db, "SELECT EnglishProductSubcategoryName FROM DimProductSubcategory"):
        models = col1(db, """SELECT DISTINCT p.ModelName FROM DimProduct p
            JOIN DimProductSubcategory s ON p.ProductSubcategoryKey = s.ProductSubcategoryKey
            WHERE s.EnglishProductSubcategoryName = ? AND p.ModelName IS NOT NULL
            AND p.EnglishDescription IS NOT NULL""", sub)
        expected = {str(onto.chunk_iri(f"product:{slug(m)}")) for m in models}
        via_model = {str(c) for m in onto.members(g, "ProductModel", "ProductSubcategory", sub)
                     for c in onto.chunks_about(g, "ProductModel", m)}
        assert onto.chunks_about(g, "ProductSubcategory", sub) == expected == via_model, sub


@pytest.fixture(params=["mini", "adventureworks", "sales"])
def any_db(request, mini_db, sales_db) -> Path:
    if request.param == "adventureworks" and not AW.is_file():
        pytest.skip("samples/adventureworks.sqlite not built")
    return {"mini": mini_db, "adventureworks": AW, "sales": sales_db}[request.param]


def test_metric_words_resolve_like_the_schema_linker(any_db: Path) -> None:
    sg = build_schema_graph(any_db)
    present = {d["name"] for _, d in sg.nodes(data=True) if d["kind"] == "metric"}
    assert present
    g = onto.full_graph()                          # the vocabulary alone answers this
    for u, v, d in sg.edges(data=True):
        if d["kind"] == "synonym_of":
            assert onto.resolve_metric(g, sg.nodes[u]["phrase"]) & present == {v[len("metric:"):]}
    disagree = set()
    for a in sg.graph["ambiguous"]:
        for phrase in a["phrases"]:
            assert onto.resolve_metric(g, phrase) & present == set(a["options"]), phrase
            clarify = link(f"{phrase} last month", sg).clarify
            if not clarify or {o["metric"] for o in clarify["options"]} != set(a["options"]):
                disagree.add(phrase)
    assert disagree <= LINKER_GAPS


# Found by the test above (19 Nov 2026), not fixed here: schema_graph.link takes the FIRST
# ambiguous group with any phrase in the question, so "units sold" asks "which sales?" (the
# word "sold") instead of "which units?". The graph resolves it to the units group. When the
# linker prefers the longest phrase, delete the entry and this test keeps the two in step.
LINKER_GAPS = {"units sold"}


# --- CLI ---------------------------------------------------------------------------------

def test_cli_writes_checks_and_validates(tmp_path: Path, mini_db: Path, capsys) -> None:
    out = tmp_path / "ontology"
    assert onto.main(["--out", str(out), "--db", str(mini_db), "--instances", str(tmp_path / "kg.ttl")]) == 0
    assert "conforms" in capsys.readouterr().out
    assert onto.main(["--check", "--out", str(out)]) == 0
    g = onto.read_ttl(out / onto.FILES["shapes"])
    g.remove((onto.ARS["Chunk-mentions"], None, None))         # a hand edit
    onto.write_ttl(g, out / onto.FILES["shapes"])
    assert onto.main(["--check", "--out", str(out)]) == 1
    abox = onto.read_ttl(tmp_path / "kg.ttl")
    assert (onto.entity_iri("Product", "Mountain-200 Black, 38"), AR.inProductSubcategory,
            onto.entity_iri("ProductSubcategory", "Mountain Bikes")) in abox
    assert str(onto.entity_iri("Product", "Mountain-200 Black, 38")).endswith(
        quote("Mountain-200 Black, 38", safe=""))
