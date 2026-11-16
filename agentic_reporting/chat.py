"""The analytics chatbot: a question → a checked answer, its SQL, and what it assumed.

    python -m agentic_reporting.chat --db samples/adventureworks.sqlite \\
        "What were internet sales of Mountain-200 Black, 38 in the Northwest in the last 10 days?"
    python -m agentic_reporting.chat --db samples/adventureworks.sqlite --provider bedrock-converse "..."
    python -m agentic_reporting.chat ... --metric internet_sales "Sales in Canada last month"

The same governed path as a scheduled report, one question at a time:

1. **Understand, in code (no LLM).** The value index turns names into exact filters
   ("Northwest" → ``DimSalesTerritory.SalesTerritoryRegion = 'Northwest'``). Time words become
   exact dates, anchored on the latest date in the data. Schema linking picks the governed
   metric, the tables and the join path (fact → dimension only).
2. **Ask back instead of guessing.** A word that names two metrics ("sales": internet or
   reseller?) returns a clarifying question with options. Any content word that matched
   nothing ("Atlas Laptop") stops the run with the closest known values: a dropped word is a
   dropped filter, and a dropped filter answers a bigger question than the one asked.
   A table that can only be reached by going back from a dimension to a fact (internet sales
   "by reseller") is refused: that join repeats rows and inflates the sum.
3. **Write SQL from the linked subset only** (``sql_writer``): one model call, guarded, the
   resolved filters and dates checked to be in the SQL (one repair), read-only execution over
   the linked tables, 200-row cap.
4. **Answer, checked.** A one-row result is answered from a template (no model call: cheaper,
   and it cannot misstate the number). A larger result is summarised by the model in the
   report's ``{text, numbers_used}`` envelope and run through the same deterministic
   checker; one repair, then the template is used instead.
5. **Show the work.** Every answer carries its SQL, the metric definition and owner, the date
   range, the filters, the data's last date, the check result, tokens and cost; each is
   appended to a hash-chained audit log.

Exit codes: 0 answered · 2 clarify / not understood · 3 refused · 4 provider unavailable.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agentic_reporting.check import check_section, row_labels
from agentic_reporting.data import QueryResult, is_number, run_query
from agentic_reporting.draft import SectionDraft
from agentic_reporting.draft import user_message as draft_message
from agentic_reporting.providers.base import ProviderUnavailable
from agentic_reporting.schema_graph import (
    DEFAULT_METRICS,
    LinkResult,
    build_schema_graph,
    link,
    tokens,
)
from agentic_reporting.sql_writer import SQLRefused, write_and_execute
from agentic_reporting.timeframe import TimeRange, resolve
from agentic_reporting.values import ValueIndex, build_value_index, filters_from

Status = Literal["answered", "clarify", "not_understood", "refused", "unavailable"]
ANSWER_MAX_TOKENS = 400
ANSWER_MAX_WORDS = 60
DEFAULT_AUDIT = Path(".agentic_reporting") / "chat_audit.jsonl"
_TOP_N_RE = re.compile(r"\b(?:top|bottom|first|best|worst|largest|smallest)\s+(\d{1,3})\b", re.I)
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")

ANSWER_SYSTEM = """You answer a business question in one or two plain sentences, from the
result of a SQL query that has already been run and checked.

Rules, in order of importance:
1. Use ONLY numbers that appear in the facts table. Never compute, estimate, sum or invent
   a number. Copy each value as given (you may drop trailing decimals only if the result
   stays within 0.5% of the fact).
2. Cite every number you write in numbers_used, as {"value": <number>, "fact_key": "<key>"},
   using the exact key from the facts table.
3. Write every number from the facts in digits. Write no other number or count, in digits
   or in words. Do not write dates: the date range is shown next to your answer.
4. Name products, regions and other labels exactly as they appear in the rows.
5. No headings, no lists, no markdown. Stay within the word limit.

Respond with ONLY a JSON object: {"text": "<answer>", "numbers_used": [{"value": <number>,
"fact_key": "<key>"}, ...]}"""


@dataclass
class ChatAnswer:
    status: Status
    question: str
    text: str
    options: list[dict[str, Any]] = field(default_factory=list)
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    assumptions: list[str] = field(default_factory=list)
    metric: dict[str, Any] | None = None
    filters: list[dict[str, Any]] = field(default_factory=list)
    time_range: dict[str, Any] | None = None
    data_as_of: str | None = None
    answer_mode: Literal["template", "model", "quote", "none"] = "none"
    route: Literal["sql", "documents", "none"] = "none"
    citations: list[dict[str, str]] = field(default_factory=list)   # documents: {"id", "title"}
    check: dict[str, Any] | None = None
    sql_repaired: bool = False
    repair_reasons: list[str] = field(default_factory=list)
    first_sql: str | None = None
    tables: list[str] = field(default_factory=list)
    join_path: list[str] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- caches: the graph and the value index are built once per database file -------------

_CACHE: dict[tuple[str, float, str], tuple[Any, ValueIndex]] = {}


def knowledge(db: str | Path, metrics: str | Path = DEFAULT_METRICS) -> tuple[Any, ValueIndex]:
    p = Path(db).resolve()
    key = (str(p), p.stat().st_mtime, str(metrics))
    if key not in _CACHE:
        g = build_schema_graph(p, metrics)
        _CACHE[key] = (g, build_value_index(p, g))
    return _CACHE[key]


# --- helpers --------------------------------------------------------------------------------

def _without_spans(question: str, spans: list[tuple[int, int]]) -> str:
    """The question's words with the value matches removed (so "Discount-2002" is no year)."""
    words = _WORD_RE.findall(question.lower())
    return " ".join(w for i, w in enumerate(words) if not any(a <= i < b for a, b in spans))


def _anchor(db: str | Path, column: str) -> date | None:
    t, c = column.split(".", 1)
    rows = run_query(db, f'SELECT MAX("{c}") FROM "{t}"').rows
    v = rows[0][0] if rows else None
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _unexplained_phrases(question: str, unexplained: list[str]) -> list[str]:
    """Group the unexplained stems back into the user's own words: "Atlas Laptop", "North"."""
    bad = set(unexplained)
    phrases, cur = [], []
    for w in _WORD_RE.findall(question):
        if set(tokens(w)) & bad:
            cur.append(w)
        elif cur:
            phrases.append(" ".join(cur))
            cur = []
    if cur:
        phrases.append(" ".join(cur))
    return [p.strip() for p in phrases if p.strip()]


def _empty(result: QueryResult) -> bool:
    return not result.rows or all(all(v is None for v in r) for r in result.rows)


def _fmt(v: Any) -> str:
    from agentic_reporting.providers.fake import fmt
    return fmt(v) if is_number(v) else str(v)


def template_answer(label: str, columns: list[str], row: list[Any]) -> str:
    """One row: "Northwest: Internet sales (USD) 1,234.5." No model call."""
    names = [str(v) for v in row if isinstance(v, str)]
    nums = [(c, v) for c, v in zip(columns, row, strict=True) if is_number(v)]
    if not nums:
        return "No data matched." if not names else ", ".join(names) + "."
    first = f"{label} {_fmt(nums[0][1])}"
    rest = [f"{c.replace('_', ' ')} {_fmt(v)}" for c, v in nums[1:]]
    lead = f"{', '.join(names)}: " if names else ""
    return lead + ", ".join([first, *rest]) + "."


def _assumptions(lr: LinkResult, anchor: date | None, tr: TimeRange | None) -> list[str]:
    out = []
    if lr.metrics:
        d = lr.metric_definitions[lr.metrics[0]]
        out.append(f"{d['label']} = {d['expression']} (owner: {d['owner']})")
    if tr:
        out.append(f"'{tr.phrase}' read as {tr.label} ({tr.rule})")
    if anchor:
        out.append(f"the data ends on {anchor.isoformat()}; relative dates count back from it")
        last_day = calendar.monthrange(anchor.year, anchor.month)[1]
        in_last_month = tr is not None and tr.start >= anchor.replace(day=1).isoformat()
        if in_last_month and tr.end == anchor.isoformat() and anchor.day < last_day:
            out.append(f"{anchor.strftime('%B %Y')} is a partial month: the data stops on day "
                       f"{anchor.day}")
    for f in lr.filters:
        vals = ", ".join(repr(v) for v in f["values"])
        out.append(f"{', '.join(repr(p) for p in f['phrases'])} read as {f['label']} "
                   f"{f['column']} = {vals}")
        for a in f["alternatives"]:
            out.append(f"  (the same words are also a {a})")
    return out


class _Audit:
    """Append-only JSONL; each line carries the sha256 of the line before it."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def write(self, answer: ChatAnswer) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prev = "0" * 64
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if lines:
                prev = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
        rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "prev": prev,
               "route": answer.route, "citations": [c["id"] for c in answer.citations],
               "question": answer.question, "status": answer.status, "sql": answer.sql,
               "metric": (answer.metric or {}).get("name"), "model": answer.model,
               "check_ok": (answer.check or {}).get("ok"), "answer_mode": answer.answer_mode,
               "rows": len(answer.rows), "cost_usd": round(answer.cost_usd, 6)}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")


# --- the pipeline ------------------------------------------------------------------------

@dataclass
class Documents:
    """The documents route. ``index=None`` is the baseline: every chunk goes in the prompt."""
    chunks: list[Any]
    provider: Any
    index: Any = None                 # doc_index.DocIndex, for mode "retrieve"
    k: int = 5

    @property
    def mode(self) -> str:
        return "retrieve" if self.index is not None else "all"


def ask(question: str, db: str | Path, sql_provider: Any, answer_provider: Any = None, *,
        metric: str | None = None, metrics_path: str | Path = DEFAULT_METRICS,
        audit: Path | None = None, documents: Documents | None = None) -> ChatAnswer:
    t0 = time.perf_counter()
    out = _ask(question, db, sql_provider, answer_provider or sql_provider, metric, metrics_path,
               documents)
    out.latency_ms = int((time.perf_counter() - t0) * 1000)
    _Audit(audit).write(out)
    return out


def _ask(question: str, db: str | Path, sql_provider: Any, answer_provider: Any,
         metric: str | None, metrics_path: str | Path,
         documents: Documents | None = None) -> ChatAnswer:
    g, idx = knowledge(db, metrics_path)
    matches = idx.match(question)
    rest = _without_spans(question, [(m.start, m.end) for m in matches])
    probe = resolve(rest, date(2000, 1, 1))            # only for which words are time words
    explained = idx.explained(matches) | set(tokens(probe.phrase) if probe else ())
    explained |= {m.group(1) for m in _TOP_N_RE.finditer(question)}
    try:
        lr = link(question, g, metric=metric, filter_columns=[m.column for m in matches],
                  explained_words=explained)
    except ValueError as e:
        return ChatAnswer("not_understood", question, str(e))

    if lr.clarify:
        return ChatAnswer("clarify", question, lr.clarify["ask"], options=[
            {"label": o["label"], "metric": o["metric"]} for o in lr.clarify["options"]])

    # Route: a governed metric means the question is about numbers in tables (SQL). No
    # metric, and documents are configured: the question goes to the documents.
    if not lr.metrics and documents is not None:
        return _documents_answer(question, matches, documents)

    if lr.unexplained:
        parts, options = [], []
        for phrase in _unexplained_phrases(question, lr.unexplained):
            sugg = idx.suggest(phrase)
            if sugg:
                parts.append(f"'{phrase}' (closest: "
                             + "; ".join(f"{s['value']} ({s['label']})" for s in sugg) + ")")
                options += [{"label": f"{s['value']} ({s['label']})",
                             "rephrase": re.sub(re.escape(phrase), s["value"], question,
                                                flags=re.I)} for s in sugg]
            else:
                parts.append(f"'{phrase}' (nothing similar in this data)")
        return ChatAnswer("not_understood", question,
                          "I could not match " + ", ".join(parts) + ". I will not guess, "
                          "because a word I ignore would be a filter I drop.", options=options)

    if not lr.metrics:
        labels = sorted(d["label"] for _, d in g.nodes(data=True) if d["kind"] == "metric")
        return ChatAnswer("not_understood", question,
                          "I can answer questions about: " + "; ".join(labels) + ".")

    if lr.unreachable:
        return ChatAnswer("refused", question,
                          f"{', '.join(lr.unreachable)} cannot be combined with "
                          f"{lr.metric_definitions[lr.metrics[0]]['label']}: the only join goes "
                          "back through a shared dimension to another fact table, which repeats "
                          "rows and inflates the total. Ask it a different way, or ask the data "
                          "owner for a governed metric that does this.",
                          tables=lr.tables)

    lr.filters = filters_from(matches)
    mdef = lr.metric_definitions[lr.metrics[0]]
    anchor = _anchor(db, mdef["time_column"]) if mdef.get("time_column") else None
    tr = resolve(rest, anchor) if anchor else None
    if tr:
        tr.column = mdef["time_column"]
        lr.time_range = tr.to_dict()
    lr.data_as_of = anchor.isoformat() if anchor else None
    base = dict(metric={"name": lr.metrics[0], **mdef}, filters=lr.filters,
                time_range=lr.time_range, data_as_of=lr.data_as_of,
                assumptions=_assumptions(lr, anchor, tr), tables=lr.tables,
                join_path=[f"{j.left_table}.{j.left_column} = {j.right_table}.{j.right_column}"
                           for j in lr.join_path])

    try:
        written, result = write_and_execute(question, lr, sql_provider, db)
    except SQLRefused as e:
        return ChatAnswer("refused", question, f"Refused: {e}", **base)
    except ProviderUnavailable as e:
        return ChatAnswer("unavailable", question, f"The model is not available: {e}", **base)
    ans = ChatAnswer("answered", question, "", route="sql", sql=written.sql,
                     columns=result.columns,
                     rows=result.rows, truncated=result.truncated, sql_repaired=written.repaired,
                     repair_reasons=written.repair_reasons, first_sql=written.first_sql,
                     model=f"{written.provider}/{written.model}",
                     input_tokens=written.input_tokens, output_tokens=written.output_tokens,
                     cost_usd=written.cost_usd, **base)
    if _empty(result):
        ans.text = (f"No {mdef['label'].lower()} matched"
                    + (f" for {tr.label}" if tr else "") + ".")
        ans.answer_mode = "template"
        return ans
    if len(result.rows) == 1:
        ans.text = template_answer(mdef["label"], result.columns, result.rows[0])
        ans.answer_mode = "template"
        return ans
    _model_answer(ans, question, result, answer_provider, lr, tr)
    return ans


def _model_answer(ans: ChatAnswer, question: str, result: QueryResult, provider: Any,
                  lr: LinkResult, tr: TimeRange | None) -> None:
    """Prose over a multi-row result, checked; one repair; else the template per row."""
    section = {"heading": "Answer", "max_words": ANSWER_MAX_WORDS,
               "prompt": f"Answer this question: {question}"}
    query = {"columns": result.columns, "rows": result.rows, "facts": result.facts,
             "truncated": result.truncated}
    labels = row_labels(result.columns, result.rows)
    names = [v for r in result.rows for v in r if isinstance(v, str)]
    names += [v for f in lr.filters for v in f["values"]]
    years = sorted(set(result.dates_ok) | ({int(tr.start[:4]), int(tr.end[:4])} if tr else set()))
    problems: list[str] | None = None
    previous = None
    for _ in range(2):
        try:
            resp = provider.complete(system=ANSWER_SYSTEM, max_tokens=ANSWER_MAX_TOKENS,
                                     schema=SectionDraft, messages=[{"role": "user", "content":
                                     draft_message(section, query, problems, previous)}])
        except (ValueError, ProviderUnavailable):
            break
        ans.input_tokens += resp.input_tokens
        ans.output_tokens += resp.output_tokens
        ans.cost_usd += resp.cost_usd
        d = resp.parsed if isinstance(resp.parsed, SectionDraft) else None
        if d is None:
            break
        used = [n.model_dump() for n in d.numbers_used]
        chk = check_section(d.text, result.facts, years, used, labels, names=names)
        ans.check = chk.to_dict()
        if chk.ok:
            ans.text, ans.answer_mode = d.text, "model"
            return
        problems, previous = chk.problems(), d.text
    lines = [template_answer(lr.metric_definitions[lr.metrics[0]]["label"], result.columns, r)
             for r in result.rows[:10]]
    more = f" (and {len(result.rows) - 10} more rows in the table)" if len(result.rows) > 10 else ""
    ans.text = " ".join(lines) + more
    ans.answer_mode = "template"


# A named model or subcategory narrows the document search; a category word (a quarter of the
# catalogue, usually just a noun: "how many bikes does the Hitch Rack carry") does not.
FILTER_COLUMNS = frozenset({"DimProduct.ModelName",
                            "DimProductSubcategory.EnglishProductSubcategoryName"})


def entity_terms(matches: list[Any], chunks: list[Any]) -> dict[str, list[str]]:
    """Value matches that narrow the document search (columns the chunks are tagged with)."""
    cols = {col for c in chunks for col in c.entities} & FILTER_COLUMNS
    filters: dict[str, list[str]] = {}
    for m in matches:
        if m.column in cols:
            filters.setdefault(m.column, []).append(m.value)
    return filters


def _documents_answer(question: str, matches: list[Any], docs: Documents) -> ChatAnswer:
    from agentic_reporting.rag import answer_from_documents
    entities = entity_terms(matches, docs.chunks)
    assumptions: list[str] = []
    embed_cost = 0.0
    if docs.index is not None:
        c0 = docs.index.embedder.cost_usd
        r = docs.index.retrieve(question, k=docs.k, entities=entities)
        embed_cost = docs.index.embedder.cost_usd - c0
        passages = [h.chunk for h in r.hits]
        assumptions.append(f"searched {len(docs.chunks)} document chunks (keyword + vector, "
                           f"rank fusion); the top {len(passages)} went to the model")
        if r.filtered_by:
            assumptions.append("only chunks about " + "; ".join(
                f"{', '.join(v)}" for v in r.filtered_by.values()))
    else:
        passages = list(docs.chunks)
        assumptions.append(f"all {len(passages)} document chunks went to the model (baseline)")
    res = answer_from_documents(question, passages, docs.provider)
    out = ChatAnswer("answered" if res.found else "not_understood", question,
                     res.text if res.found else
                     "I could not find this in the data or the documents.",
                     assumptions=assumptions, answer_mode=res.answer_mode, route="documents",
                     citations=res.citations, check=res.check, model=res.model,
                     input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                     cost_usd=res.cost_usd + embed_cost)
    return out


def documents_for(db: str | Path, mode: str, provider: Any, embedder_name: str = "fake",
                  index_dir: Path = Path(".agentic_reporting")) -> Documents | None:
    """Load the document chunks for ``db``; for mode "retrieve", sync the index first."""
    if mode == "off":
        return None
    from agentic_reporting.documents import load_chunks
    chunks, _ = load_chunks(db)
    if not chunks:
        return None
    if mode == "all":
        return Documents(chunks, provider)
    from agentic_reporting.doc_index import DocIndex
    from agentic_reporting.embeddings import get_embedder
    emb = get_embedder(embedder_name)
    index = DocIndex(index_dir / f"docs_{Path(db).stem}_{embedder_name}.sqlite", emb)
    index.sync(chunks)
    return Documents(chunks, provider, index)


# --- CLI ---------------------------------------------------------------------------------

def get_providers(name: str, db: str | Path) -> tuple[Any, Any]:
    if name == "fake":
        from agentic_reporting.providers.fake import FactsFakeProvider, FakeSQLProvider
        return FakeSQLProvider(extra=golden_sql(db)), FactsFakeProvider()
    from agentic_reporting.providers.router import get_provider
    p = get_provider(name)
    return p, p


def golden_sql(db: str | Path) -> dict[str, tuple[str, list[str]]]:
    """The reference SQL of the golden chat questions for this sample (the offline fake)."""
    path = (Path(__file__).resolve().parent.parent / "eval" / "datasets" / "golden"
            / f"chat_questions_{Path(db).stem}.json")
    if not path.is_file():
        return {}
    from agentic_reporting.providers.fake import question_key
    data = json.loads(path.read_text(encoding="utf-8"))
    return {question_key(q["question"]): (q["reference_sql"], [q["expected_metric"]])
            for q in data["questions"] if q.get("reference_sql")}


def render(a: ChatAnswer) -> str:
    from agentic_reporting.ask import pretty_sql, table
    lines = [f"[{a.status}] {a.text}"]
    for o in a.options:
        lines.append(f"   option: {o['label']}"
                     + (f"   (--metric {o['metric']})" if "metric" in o else ""))
    for s in a.assumptions:
        lines.append(f"   assumed: {s}")
    if a.join_path:
        lines.append(f"   joins: {'; '.join(a.join_path)}")
    for c in a.citations:
        lines.append(f"   source: [{c['id']}] {c['title']}")
    if a.sql:
        lines.append(f"   sql ({a.model}{', repaired once' if a.sql_repaired else ''}):")
        lines.append(pretty_sql(a.sql))
    if a.columns:
        lines.append(f"   result ({len(a.rows)} rows{', capped' if a.truncated else ''}):")
        lines.append(table(a.columns, a.rows))
    if a.check:
        lines.append(f"   answer check: {'ok' if a.check['ok'] else 'FAILED'}, "
                     f"{a.check['numbers_checked']} numbers checked ({a.answer_mode})")
    elif a.status == "answered" and a.route != "documents":
        lines.append(f"   answer: {a.answer_mode} (no model call for the prose)")
    lines.append(f"   tokens {a.input_tokens} in / {a.output_tokens} out · ${a.cost_usd:.6f} · "
                 f"{a.latency_ms} ms")
    return "\n".join(lines)


EXIT = {"answered": 0, "clarify": 2, "not_understood": 2, "refused": 3, "unavailable": 4}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ask the analytics chatbot one question.")
    ap.add_argument("question")
    ap.add_argument("--db", required=True)
    ap.add_argument("--provider", default="fake",
                    help="fake (golden questions only) | bedrock-converse | bedrock | anthropic | ollama")
    ap.add_argument("--metric", help="answer a clarifying question: the governed metric to use")
    ap.add_argument("--json", action="store_true", help="print the answer as JSON")
    ap.add_argument("--audit", default=str(DEFAULT_AUDIT), help="audit log ('' to disable)")
    ap.add_argument("--docs", default="retrieve", choices=["retrieve", "all", "off"],
                    help="documents route: hybrid retrieval (default), the whole corpus in the "
                         "prompt (baseline), or off")
    ap.add_argument("--embedder", default="fake", help="fake | titan | cohere | cohere-multilingual")
    args = ap.parse_args(argv)
    sql_p, ans_p = get_providers(args.provider, args.db)
    if args.provider == "fake":
        from agentic_reporting.rag import FakeRagProvider
        doc_p: Any = FakeRagProvider()
    else:
        doc_p = sql_p
    docs = documents_for(args.db, args.docs, doc_p, args.embedder)
    a = ask(args.question, args.db, sql_p, ans_p, metric=args.metric,
            audit=Path(args.audit) if args.audit else None, documents=docs)
    print(json.dumps(a.to_dict(), indent=2, default=str) if args.json else render(a))
    return EXIT[a.status]


if __name__ == "__main__":
    sys.exit(main())
