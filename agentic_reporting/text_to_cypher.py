"""Question → Cypher → rows, governed: the LLM writes the query text and nothing else.

    schema = build_schema()                                     # labels + types, from the TBox
    names = resolve(question, idx, spec)                        # values found in code, no LLM
    w = write_cypher(question, names, schema, provider)         # ONE structured call (+1 repair)
    result = run_read(driver, w.cypher, ns="AgenticKG")         # read transaction, timeout, row cap

The model sees the graph schema (generated from the ontology) and the names the value index
resolved in the question. Its Cypher is refused (``CypherRefused``) before it reaches the
database unless ``guard`` passes:

1. one read-only statement: it starts with MATCH, OPTIONAL MATCH, WITH, UNWIND or RETURN and
   has a RETURN; no ``;``, no comments, no ``$`` parameters, no backtick-quoted names;
2. no write or admin clause: CREATE, MERGE, DELETE, DETACH, SET, REMOVE, DROP, LOAD CSV,
   FOREACH, CALL (procedures and subqueries alike), USE, SHOW, GRANT, ... and no procedure or
   function namespace ``apoc.``, ``dbms.``, ``db.``, ``gds.``;
3. every label in a node pattern and every type in a relationship pattern is in the schema;
   node label expressions are plain (``:A:B``, no ``|``, ``&``, ``!``, ``%``);
4. every variable-length path has an upper bound of at most ``MAX_HOPS``; no unbounded
   quantified path pattern;
5. every hop joins classes the schema joins: ``(:Reseller)-[:LOCATED_IN]->(:SalesTerritoryCountry)``
   is refused (LOCATED_IN goes to a StateProvince); a variable-length hop must be able to reach
   its end class over the listed types within its bounds (``path_violations``);
6. every value the question named (resolved in code) appears in it as a string literal, every
   name it filters on (``{name: '...'}``, ``x.name = '...'``) is one of those values, on a node
   of the class it was resolved to (``name_violations``). A miss gets one repair round-trip
   that names it, then the question is refused. This is the dropped-filter check of
   ``sql_writer.py``, for Cypher, plus a check that no filter value is invented.

``run_read`` then adds the namespace label to every labelled node pattern (the query sees one
loaded graph only), and runs it in a **read transaction** (the server refuses a write there:
``Neo.ClientError.Statement.AccessMode``), with a server-side timeout and a row cap. Neo4j
Community has no role-based access control, so read-only is enforced by the guard and the
read transaction together; a read-only database user would be a third layer on Enterprise
or Aura.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from agentic_reporting.graphdb import DEFAULT_NS, GraphSchema, ident
from agentic_reporting.providers.base import Provider

CYPHER_MAX_TOKENS = 600
QUESTION_MARKER = "QUESTION:"          # the same marker as sql_writer, so the fakes read it alike
NAMES_MARKER = "RESOLVED_NAMES_JSON:"
REPAIR_MARKER = "REPAIR:"
MAX_HOPS = 6
ROW_CAP = 200
TIMEOUT_S = 5.0

START = re.compile(r"^\s*(OPTIONAL\s+MATCH|MATCH|WITH|UNWIND|RETURN)\b", re.I)
FORBIDDEN = ("CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "LOAD", "FOREACH", "CALL",
             "USE", "SHOW", "GRANT", "DENY", "REVOKE", "ALTER", "RENAME", "TERMINATE", "START", "STOP",
             "ENABLE", "DISABLE", "INSERT", "FINISH")
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.I)
NAMESPACE_RE = re.compile(r"\b(apoc|dbms|db|gds|genai)\s*\.", re.I)
STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", re.S)
NODE_RE = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?\s*(:[^\s{)]*(?:\s*[:|&!%]\s*[^\s{)]*)*)")
REL_RE = re.compile(r"\[\s*(?:[A-Za-z_]\w*)?\s*(:[^\]{*]*)?(\*[^\]{]*)?")
# ((a)-[:R]->(b))+ or ){2,}: a quantified path pattern with no upper bound (not count(x) + 1)
UNBOUNDED_QPP = re.compile(r"\)\s*\{\s*\d*\s*,\s*\}|\)\)\s*[+*](?=\s*(?:[(<\-,]|$|[A-Za-z]))")

SYSTEM_PROMPT = """You write one read-only Cypher query (Neo4j 5) that answers a question about
a business knowledge graph. The graph holds names and how they relate; it holds no sales
numbers.

{schema}

Rules, in order of importance:
1. One read-only query: MATCH ... RETURN. Never CREATE, MERGE, DELETE, SET, REMOVE, CALL,
   LOAD CSV or FOREACH. No parameters ($), no comments, no semicolon.
2. Use only the labels and relationship types listed above, exactly in the direction shown.
   Every relationship joins the two labels shown for it and no others.
3. Match a named value by its exact name, with the class it was resolved to:
   (c:ProductCategory {{name: 'Bikes'}}). Use every resolved name, spelled exactly as given.
4. Hierarchy relationships point from child to parent. To find what lies under a parent, start
   at the child and walk up: MATCH (x:Child)-[:{hierarchy}*1..{depth}]->(p:Parent {{name: '...'}}).
   To find a parent, start at the child and walk up the same way.
5. Return names (x.name), never whole nodes. Use RETURN DISTINCT for a list, and
   count(DISTINCT x) for "how many". For "which ... most", ORDER BY the count DESC LIMIT 1.

Examples (other questions, same graph):
{examples}

Respond with ONLY a JSON object: {{"cypher": "<one query>", "rationale": "<one sentence>"}}"""

# Shown to the model. tests/test_text_to_cypher.py runs each one through the guard and Neo4j, so
# an example cannot drift from the schema.
EXAMPLES = [
    ("Which product models are in the Clothing category?",
     "MATCH (m:ProductModel)-[:IN_PRODUCT_SUBCATEGORY|IN_PRODUCT_CATEGORY*1..2]->"
     "(:ProductCategory {name: 'Clothing'}) RETURN DISTINCT m.name"),
    ("How many resellers in Ontario are a Value Added Reseller?",
     "MATCH (r:Reseller)-[:LOCATED_IN]->(:StateProvince {name: 'Ontario'}) "
     "MATCH (r)-[:HAS_BUSINESS_TYPE]->(:ResellerBusinessType {name: 'Value Added Reseller'}) "
     "RETURN count(DISTINCT r)"),
]


class CypherAnswer(BaseModel):
    cypher: str = Field(min_length=1)
    rationale: str = ""


class CypherRefused(ValueError):
    """The Cypher broke a rule; it was not executed."""


@dataclass
class WrittenCypher:
    cypher: str
    rationale: str
    provider: str
    model: str
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    repaired: bool = False
    repair_reasons: list[str] = field(default_factory=list)
    first_cypher: str | None = None


@dataclass
class CypherResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    cypher: str                    # as executed (namespace label added)


# --- the guard ------------------------------------------------------------------------------

def mask_strings(cypher: str) -> str:
    """String literals blanked (same length), so keywords inside a name never trip the guard."""
    return STRING_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], cypher)


def string_literals(cypher: str) -> set[str]:
    out = set()
    for m in STRING_RE.finditer(cypher):
        body = m.group(0)[1:-1]
        out.add(re.sub(r"\\(.)", r"\1", body))
    return out


def _names(expr: str, seps: str) -> list[str]:
    return [p.strip() for p in re.split(f"[{re.escape(seps)}]", expr) if p.strip()]


def violations(cypher: str, schema: GraphSchema) -> list[str]:
    """Every rule the query breaks (empty = it may run)."""
    text = cypher.strip()
    masked = mask_strings(text)
    out: list[str] = []
    if not START.match(masked):
        out.append("it must start with MATCH, OPTIONAL MATCH, WITH, UNWIND or RETURN")
    if not re.search(r"\bRETURN\b", masked, re.I):
        out.append("it has no RETURN")
    for bad, why in ((";", "a semicolon (one statement only)"), ("//", "a comment"), ("/*", "a comment"),
                     ("`", "a backtick-quoted name"), ("$", "a parameter ($); write values as literals")):
        if bad in masked:
            out.append(f"it contains {why}")
    words = sorted({m.group(1).upper() for m in FORBIDDEN_RE.finditer(masked)})
    if words:
        out.append("it uses a write or admin clause: " + ", ".join(words))
    spaces = sorted({m.group(1).lower() for m in NAMESPACE_RE.finditer(masked)})
    if spaces:
        out.append("it calls a procedure or function namespace: " + ", ".join(spaces))
    known_labels, known_types = schema.all_labels(), set(schema.rel_types)
    for m in NODE_RE.finditer(masked):
        expr = m.group(1)
        if re.search(r"[|&!%]", expr):
            out.append(f"node label expression {expr.strip()!r}: use plain labels (:A:B)")
            continue
        unknown = [lab for lab in _names(expr, ":") if lab not in known_labels]
        if unknown:
            out.append(f"unknown label(s) {unknown}; known: {sorted(known_labels)}")
    for m in REL_RE.finditer(masked):
        types, hops = m.group(1), m.group(2)
        if types:
            if "!" in types or "&" in types or "%" in types:
                out.append(f"relationship type expression {types.strip()!r}: use TYPE or TYPE1|TYPE2")
            unknown = [t for t in _names(types, ":|!&%") if t not in known_types]
            if unknown:
                out.append(f"unknown relationship type(s) {unknown}; known: {sorted(known_types)}")
        if hops is not None:
            h = re.fullmatch(r"\*\s*(\d*)\s*(?:(\.\.)\s*(\d*))?\s*", hops)
            if not h:
                out.append(f"variable-length {hops.strip()!r} is not *n or *min..max")
                continue
            lo, dots, hi = h.groups()
            upper = (int(hi) if hi else None) if dots else (int(lo) if lo else None)
            if upper is None or upper > MAX_HOPS:
                out.append(f"variable-length path {hops.strip()!r} needs an upper bound of at most "
                           f"{MAX_HOPS}, e.g. *1..{schema.max_depth}")
    if UNBOUNDED_QPP.search(masked):
        out.append(f"quantified path pattern without an upper bound (at most {MAX_HOPS})")
    if not out:
        out += path_violations(masked, schema)
    return out


# --- typed paths: every hop joins classes the schema joins --------------------------------------

PATH_NODE = re.compile(r"\(\s*([A-Za-z_]\w*)?\s*((?:\s*:\s*[A-Za-z_]\w*)*)\s*(?:\{[^{}]*\})?\s*\)")
PATH_REL = re.compile(r"\s*(<)?-\s*(?:\[\s*(?:[A-Za-z_]\w*)?\s*(?::\s*([\w|:\s]+?))?\s*"
                      r"(\*\s*\d*\s*(?:\.\.\s*\d*)?)?\s*(?:\{[^{}]*\})?\s*\])?\s*-(>)?\s*")


def _compatible(a: str, x: str, schema: GraphSchema) -> bool:
    """Label ``a`` in a query can stand for class ``x`` (the same, a superclass, or a subclass)."""
    return a == x or a in schema.labels.get(x, [x]) or x in schema.labels.get(a, [a])


def _edge_set(schema: GraphSchema) -> set[tuple[str, str, str]]:
    edges = set(schema.edges)
    typed = {t for _, t, _ in edges}
    edges |= {(a, t, b) for t, (a, b) in schema.rel_types.items() if t not in typed}
    return edges


def _hops(spec: str | None) -> tuple[int, int]:
    if not spec:
        return 1, 1
    m = re.fullmatch(r"\*\s*(\d*)\s*(?:(\.\.)\s*(\d*))?", spec.strip())
    lo, dots, hi = m.groups() if m else ("", None, "")
    if not dots:
        n = int(lo) if lo else 1
        return (n, n) if lo else (1, MAX_HOPS)
    return (int(lo) if lo else 1), (int(hi) if hi else MAX_HOPS)


def _reachable(a: str, b: str, types: set[str] | None, lo: int, hi: int, schema: GraphSchema) -> bool:
    """Is there a class path a → b of lo..hi hops over ``types`` (None = any type)?"""
    edges = [e for e in _edge_set(schema) if types is None or e[1] in types]
    frontier = {a}
    for n in range(hi + 1):
        if n >= lo and any(_compatible(b, c, schema) or _compatible(c, b, schema) for c in frontier):
            return True
        frontier = {y for c in frontier for x, _, y in edges if _compatible(c, x, schema)}
        if not frontier:
            return False
    return False


def path_violations(masked: str, schema: GraphSchema) -> list[str]:
    """Hops whose two end labels the schema never joins by that type: ``(:Reseller)-[:LOCATED_IN]->
    (:SalesTerritoryCountry)`` is refused because LOCATED_IN goes to a StateProvince."""
    nodes = list(PATH_NODE.finditer(masked))
    var_label: dict[str, str] = {}
    for m in nodes:
        labels = _names(m.group(2) or "", ":")
        if m.group(1) and labels:
            var_label.setdefault(m.group(1), labels[0])

    def label(m: re.Match) -> str | None:
        labels = _names(m.group(2) or "", ":")
        return labels[0] if labels else var_label.get(m.group(1) or "")

    known = schema.all_labels()
    out = []
    for left, right in zip(nodes, nodes[1:], strict=False):
        rel = PATH_REL.fullmatch(masked, left.end(), right.start())
        if not rel:
            continue
        a, b = label(left), label(right)
        if not a or not b or a not in known or b not in known:
            continue
        back, types, hops, fwd = rel.group(1), rel.group(2), rel.group(3), rel.group(4)
        tset = set(_names(types, ":|")) if types else None
        lo, hi = _hops(hops)
        if back and not fwd:
            a, b = b, a
        ok = _reachable(a, b, tset, lo, hi, schema) or (not back and not fwd and
                                                        _reachable(b, a, tset, lo, hi, schema))
        if not ok:
            shown = "|".join(sorted(tset)) if tset else ""
            hint = "; ".join(f"{t} goes (:{x})->(:{y})" for x, t, y in sorted(_edge_set(schema))
                             if tset and t in tset)
            out.append(f"no path (:{a})-[:{shown}{hops or ''}]->(:{b}) in the schema"
                       + (f" ({hint})" if hint else ""))
    return out


def guard(cypher: str, schema: GraphSchema) -> str:
    bad = violations(cypher, schema)
    if bad:
        raise CypherRefused("; ".join(bad))
    return cypher.strip()


def scope(cypher: str, ns: str = DEFAULT_NS) -> str:
    """Add the namespace label to every labelled node pattern (``(p:Product`` → ``(p:Product:NS``)."""
    ns = ident(ns)
    masked = mask_strings(cypher)
    out, last = [], 0
    for m in NODE_RE.finditer(masked):
        end = m.end(1)
        out.append(cypher[last:end] + f":{ns}")
        last = end
    out.append(cypher[last:])
    return "".join(out)


def missing_names(cypher: str, names: list[dict[str, Any]]) -> list[str]:
    lits = string_literals(cypher)
    return [n["value"] for n in names if n["value"] not in lits]


_LIT = "(" + STRING_RE.pattern + ")"
NAME_IN_NODE = re.compile(r"\bname\s*:\s*" + _LIT)
NAME_IN_WHERE = re.compile(r"\b([A-Za-z_]\w*)\.name\s*=\s*" + _LIT)


def _unquote(lit: str) -> str:
    return re.sub(r"\\(.)", r"\1", lit[1:-1])


def name_uses(cypher: str) -> list[tuple[str | None, str]]:
    """(class label or None, value) for every name the query filters on: ``{name: '...'}`` in a
    node pattern, or ``x.name = '...'`` where ``x`` is bound to a label."""
    masked = mask_strings(cypher)
    nodes = list(PATH_NODE.finditer(masked))
    var_label: dict[str, str] = {}
    for m in nodes:
        labels = _names(m.group(2) or "", ":")
        if m.group(1) and labels:
            var_label.setdefault(m.group(1), labels[0])
    out: list[tuple[str | None, str]] = []
    for m in nodes:
        labels = _names(m.group(2) or "", ":")
        label = labels[0] if labels else var_label.get(m.group(1) or "")
        for lit in NAME_IN_NODE.finditer(cypher, m.start(), m.end()):
            out.append((label, _unquote(lit.group(1))))
    for lit in NAME_IN_WHERE.finditer(cypher):
        if not any(n.start() <= lit.start() < n.end() for n in nodes):
            out.append((var_label.get(lit.group(1)), _unquote(lit.group(2))))
    return out


def name_violations(cypher: str, names: list[dict[str, Any]], schema: GraphSchema) -> list[str]:
    """A name the question did not contain, or a name bound to the wrong class, is refused:
    the model may not invent a filter value or move it to another class."""
    resolved: dict[str, set[str]] = {}
    for n in names:
        resolved.setdefault(n["value"], set()).update([n["class"], *n.get("also_a", [])])
    out = []
    for label, value in name_uses(cypher):
        if value not in resolved:
            out.append(f"the name {value!r} is not one the question named (resolved: {sorted(resolved)})")
        elif label and label in schema.all_labels() and not any(
                label in schema.labels.get(c, [c]) for c in resolved[value]):
            out.append(f"{value!r} is a {' or '.join(sorted(resolved[value]))}, not a {label}")
    return out


# --- names resolved in code ---------------------------------------------------------------

def resolve(question: str, idx: Any, spec: Any) -> list[dict[str, Any]]:
    """The values the value index finds in the question, with their ontology class."""
    out = []
    for m in idx.match(question):
        alts = []
        for a in m.alternatives:
            col = a.split(": ", 1)[-1]
            if col in spec.column_class:
                alts.append(spec.column_class[col])
        out.append({"value": m.value, "class": spec.column_class.get(m.column, "?"), "words": m.phrase,
                    "also_a": alts})
    return out


# --- the model call ---------------------------------------------------------------------------

def system_prompt(schema: GraphSchema) -> str:
    examples = "\n".join(f"  Q: {q}\n  {json.dumps({'cypher': c})}" for q, c in EXAMPLES)
    return SYSTEM_PROMPT.format(schema=schema.describe(), hierarchy=schema.hierarchy_pattern(),
                                depth=schema.max_depth, examples=examples)


def user_message(question: str, names: list[dict[str, Any]]) -> str:
    return "\n".join([QUESTION_MARKER, question.strip(), "", NAMES_MARKER, json.dumps(names, sort_keys=True)])


def _ask(provider: Provider, schema: GraphSchema, messages: list[dict]) -> tuple[CypherAnswer, Any]:
    resp = provider.complete(system=system_prompt(schema), messages=messages,
                             max_tokens=CYPHER_MAX_TOKENS, schema=CypherAnswer)
    answer = (resp.parsed if isinstance(resp.parsed, CypherAnswer)
              else CypherAnswer.model_validate_json(resp.text))
    return answer, resp


def _problems(cypher: str, schema: GraphSchema, names: list[dict[str, Any]]) -> list[str]:
    out = violations(cypher, schema)
    out += [f"the name {v!r} is not used as a string literal" for v in missing_names(cypher, names)]
    out += name_violations(cypher, names, schema)
    return out


def write_cypher(question: str, names: list[dict[str, Any]], schema: GraphSchema, provider: Provider,
                 error: tuple[str, str] | None = None) -> WrittenCypher:
    """One structured call; at most one repair that names every broken rule; raises ``CypherRefused``.

    ``error=(cypher, message)``: the previous query failed in Neo4j; the model sees the error once.
    """
    messages = [{"role": "user", "content": user_message(question, names)}]
    if error:
        messages += [{"role": "assistant", "content": json.dumps({"cypher": error[0]})},
                     {"role": "user", "content": f"{REPAIR_MARKER} Neo4j rejected that query: "
                      f"{error[1][:300]}. Rewrite it and return the JSON object only."}]
    answer, resp = _ask(provider, schema, messages)
    cost, tin, tout = resp.cost_usd, resp.input_tokens, resp.output_tokens
    problems, first, repaired = _problems(answer.cypher, schema, names), answer.cypher, False
    reasons = list(problems)
    if problems:
        repaired = True
        messages += [{"role": "assistant", "content": answer.model_dump_json()},
                     {"role": "user", "content": REPAIR_MARKER + " your query breaks these rules: "
                      + "; ".join(problems) + ". Rewrite it and return the JSON object only."}]
        answer, resp = _ask(provider, schema, messages)
        cost, tin, tout = cost + resp.cost_usd, tin + resp.input_tokens, tout + resp.output_tokens
        problems = _problems(answer.cypher, schema, names)
    if problems:
        raise CypherRefused(f"{resp.provider}/{resp.model} (after one repair): " + "; ".join(problems))
    return WrittenCypher(answer.cypher.strip(), answer.rationale, resp.provider, resp.model, cost, tin, tout,
                         repaired, reasons, first if repaired else None)


# --- read-only execution ------------------------------------------------------------------------

def run_read(driver: Any, cypher: str, schema: GraphSchema, ns: str = DEFAULT_NS,
             database: str | None = None, row_cap: int = ROW_CAP,
             timeout_s: float = TIMEOUT_S) -> CypherResult:
    """Guard, scope to the namespace, run in a read transaction with a timeout; at most ``row_cap`` rows."""
    from neo4j import READ_ACCESS, unit_of_work

    from agentic_reporting.graphdb import _db

    scoped = scope(guard(cypher, schema), ns)

    @unit_of_work(timeout=timeout_s)
    def work(tx: Any) -> tuple[list[str], list[list[Any]]]:
        result = tx.run(scoped)
        records = result.fetch(row_cap + 1)
        keys = list(result.keys())
        result.consume()
        return keys, [list(r.values()) for r in records]

    with driver.session(database=_db(database), default_access_mode=READ_ACCESS) as session:
        keys, rows = session.execute_read(work)
    return CypherResult(keys, rows[:row_cap], len(rows) > row_cap, scoped)


def ask(question: str, provider: Provider, driver: Any, schema: GraphSchema, idx: Any, spec: Any,
        ns: str = DEFAULT_NS, database: str | None = None) -> tuple[WrittenCypher, CypherResult]:
    """``write_cypher`` then ``run_read``; a Neo4j error (bad syntax) gets one repair turn."""
    from neo4j.exceptions import ClientError

    names = resolve(question, idx, spec)
    written = write_cypher(question, names, schema, provider)
    try:
        return written, run_read(driver, written.cypher, schema, ns, database)
    except ClientError as e:
        err = (written.cypher, f"{e.code}: {e.message}")
    again = write_cypher(question, names, schema, provider, error=err)
    again.cost_usd += written.cost_usd
    again.input_tokens += written.input_tokens
    again.output_tokens += written.output_tokens
    again.repaired = True
    again.repair_reasons = [*written.repair_reasons, f"neo4j: {err[1][:120]}", *again.repair_reasons]
    again.first_cypher = written.first_cypher or written.cypher
    try:
        return again, run_read(driver, again.cypher, schema, ns, database)
    except ClientError as e:
        raise CypherRefused(f"the query failed in Neo4j twice: {e.message}") from e
