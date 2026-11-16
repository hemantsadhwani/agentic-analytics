"""Render the checked report as one self-contained HTML page, built for a two-minute read.

Order on the page: headline tiles and a two-line summary, then "Needs a decision", then one
card per section with its chart, its checked prose and its numbers as a table.

**Every number on the page is a query value.** Tiles, chart marks, axis ticks and tables are
drawn by this module from the section's rows; the model only writes the prose, and the prose
goes through the same checker as the Markdown report (``render.checked_section``): an
unsupported number is replaced with ``[unverified]``. The rules that decide what "needs a
decision" are the section's SQL, not the model.

**Self-contained.** Inline CSS, inline SVG, one small inline script for hover tooltips. No
fonts, scripts or images from the network, so the page works in private mode, attaches to an
email, and prints. Light and dark themes follow the reader's system setting.

Chart rules (thin marks, one axis, legend for two or more series, colours validated for
colour-blind readers, a table twin for every chart) follow the data-viz method in
``docs/guide``; text never wears a series colour.
"""

from __future__ import annotations

import html
import json
import math
from typing import Any

from agentic_reporting.data import is_number
from agentic_reporting.render import MARK, checked_section

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
SEVERITY = {"critical": ("Critical", "▲"), "warning": ("Warning", "●"), "info": ("Note", "○")}
W = 500                                   # chart viewBox width, close to a half-page card, so text
                                          # renders near its set size


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


# --- numbers ------------------------------------------------------------------------------

def _month(v: Any) -> tuple[int, int] | None:
    s = str(v)
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit():
        return int(s[:4]), int(s[5:])
    return None


def month_label(v: Any, long: bool = False) -> str:
    ym = _month(v)
    if not ym:
        return str(v)
    y, m = ym
    return f"{MONTH_NAMES[m - 1]} {y}" if long else f"{MONTHS[m - 1]} {y}"


def shift_month(v: Any, months: int) -> str | None:
    ym = _month(v)
    if not ym:
        return None
    n = ym[0] * 12 + ym[1] - 1 + months
    return f"{n // 12}-{n % 12 + 1:02d}"


def compact(v: float, fmt: str) -> str:
    """Auto-compact: 1,284 / 12.9K / $4.2M. For tiles and axis ticks."""
    if fmt == "pct":
        return f"{v:.1f}%"
    if fmt == "pts":
        return f"{v:+.1f} pts"
    a = abs(v)
    if a == 0:
        return "$0" if fmt == "usd" else "0"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div and a >= 10_000:              # in full under 10,000: 2,267 not 2.27K
            s = f"{v / div:.2f}".rstrip("0").rstrip(".") if a < 10 * div else f"{v / div:.1f}".rstrip("0").rstrip(".")
            return f"{'$' if fmt == 'usd' else ''}{s}{suf}"
    s = f"{v:,.0f}" if fmt != "usd" or a >= 100 else f"{v:,.2f}"
    return f"{'$' if fmt == 'usd' else ''}{s}"


def full(v: Any, fmt: str) -> str:
    """The query value as written, with separators: for tables and tooltips."""
    if v is None:
        return "–"
    if not is_number(v):
        return str(v)
    if fmt == "usd":
        return f"${v:,.2f}"
    if fmt == "pct":
        return f"{v:,.1f}%"
    if fmt == "pts":
        return f"{v:+,.1f} pts"
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}".rstrip("0").rstrip(".")


def column_format(col: str, default: str = "count") -> str:
    c = col.lower()
    if c.endswith("_pts"):
        return "pts"
    if c.endswith("_pct") or c.endswith("_share"):
        return "pct"
    if any(w in c for w in ("sales", "revenue", "amount", "cost", "margin_usd", "price")):
        return "usd"
    return default


def nice_ticks(hi: float, n: int = 4) -> list[float]:
    """0 to a clean maximum in about ``n`` steps (1, 2, 2.5, 5 x 10^k)."""
    if hi <= 0:
        return [0.0, 1.0]
    raw = hi / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = math.ceil(hi / step) * step
    return [round(i * step, 10) for i in range(int(round(top / step)) + 1)]


# --- charts -------------------------------------------------------------------------------

def _tip(title: str, rows: list[tuple[str, str, str]]) -> str:
    """Tooltip payload: title, then (value, series name, series class) rows."""
    return esc(json.dumps({"t": title, "r": rows}, ensure_ascii=False))


def _legend(names: list[str], classes: list[str], kind: str) -> str:
    if len(names) < 2:
        return ""
    items = "".join(f'<li><span class="key {kind} {c}"></span>{esc(n)}</li>'
                    for n, c in zip(names, classes, strict=True))
    return f'<ul class="legend">{items}</ul>'


def _bar_path(x0: float, y0: float, w: float, h: float, r: float = 4.0, horizontal: bool = True) -> str:
    """A bar square at the baseline with a rounded data end."""
    if horizontal:
        r = min(r, w, h / 2)
        return (f"M{x0:.1f},{y0:.1f}h{max(w - r, 0):.1f}a{r},{r} 0 0 1 {r},{r}"
                f"v{h - 2 * r:.1f}a{r},{r} 0 0 1 -{r},{r}h-{max(w - r, 0):.1f}z")
    r = min(r, h, w / 2)        # vertical: y0 is the top of the bar, baseline at y0 + h
    return (f"M{x0:.1f},{y0 + h:.1f}v-{max(h - r, 0):.1f}a{r},{r} 0 0 1 {r},-{r}"
            f"h{w - 2 * r:.1f}a{r},{r} 0 0 1 {r},{r}v{max(h - r, 0):.1f}z")


def line_chart(cols: list[str], rows: list[list[Any]], ch: dict[str, Any]) -> str:
    xi, ys = cols.index(ch["x"]), [cols.index(y) for y in ch["y"]]
    fmt = ch.get("format", "count")
    names = [ch.get("labels", {}).get(y, y) for y in ch["y"]]
    classes = [("s1" if i == 0 else "ctx") if ch.get("style") == "emphasis" else f"s{i + 1}"
               for i in range(len(ys))]
    vals = [[r[j] if is_number(r[j]) else None for r in rows] for j in ys]
    hi = max((v for s in vals for v in s if v is not None), default=0.0)
    ticks = nice_ticks(hi)
    L, R, T, B = 52, 76, 14, 30
    H = 240
    pw, ph = W - L - R, H - T - B
    n = len(rows)

    def X(i: int) -> float:
        return L + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v: float) -> float:
        return T + ph - ph * v / ticks[-1]

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{esc(", ".join(names))} by {esc(ch["x"])}">']
    for t in ticks:
        out.append(f'<line class="grid" x1="{L}" x2="{L + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>')
        out.append(f'<text class="tick" x="{L - 8}" y="{Y(t) + 4:.1f}" text-anchor="end">{esc(compact(t, fmt))}</text>')
    out.append(f'<line class="axis" x1="{L}" x2="{L + pw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    step = 1 if n <= 7 else 2
    for i, r in enumerate(rows):
        if i % step and i != n - 1:
            continue
        ym = _month(r[xi])
        lab = (MONTHS[ym[1] - 1] + (f" {str(ym[0])[2:]}" if i == 0 or ym[1] == 1 or i == n - 1 else "")
               ) if ym else str(r[xi])
        out.append(f'<text class="tick" x="{X(i):.1f}" y="{H - 10}" text-anchor="middle">{esc(lab)}</text>')
    for s, c in zip(vals, classes, strict=True):
        pts = [(X(i), Y(v)) for i, v in enumerate(s) if v is not None]
        if pts:
            d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            out.append(f'<path class="line {c}" d="{d}"/>')
    # end markers and end labels (value at the end; skipped when two labels would collide)
    ends = [(s[-1], c, nm) for s, c, nm in zip(vals, classes, names, strict=True) if s and s[-1] is not None]
    ys_end = sorted(Y(v) for v, _, _ in ends)
    collide = any(b - a < 26 for a, b in zip(ys_end, ys_end[1:], strict=False))
    for v, c, nm in ends:
        out.append(f'<circle class="dot {c}" cx="{X(n - 1):.1f}" cy="{Y(v):.1f}" r="4"/>')
        if not collide:
            out.append(f'<text class="endlab" x="{X(n - 1) + 10:.1f}" y="{Y(v) - 2:.1f}">{esc(compact(v, fmt))}</text>'
                       f'<text class="endname" x="{X(n - 1) + 10:.1f}" y="{Y(v) + 12:.1f}">{esc(nm)}</text>')
    out.append(f'<line class="crosshair" x1="0" x2="0" y1="{T}" y2="{T + ph}" visibility="hidden"/>')
    for i, r in enumerate(rows):       # hit columns: the crosshair finds the X
        x0 = X(i) - (pw / (n - 1) / 2 if n > 1 else pw / 2)
        tip = _tip(month_label(r[xi], long=True),
                   [(full(s[i], fmt), nm, c) for s, nm, c in zip(vals, names, classes, strict=True)])
        out.append(f'<rect class="hit" tabindex="0" x="{max(x0, L - 4):.1f}" y="{T}" width="{pw / max(n - 1, 1):.1f}" '
                   f'height="{ph}" data-x="{X(i):.1f}" data-tip="{tip}"/>')
    out.append("</svg>")
    return _legend(names, classes, "line") + "".join(out)


def hbar_chart(cols: list[str], rows: list[list[Any]], ch: dict[str, Any]) -> str:
    xi, ys = cols.index(ch["x"]), [cols.index(y) for y in ch["y"]]
    fmt = ch.get("format", "count")
    names = [ch.get("labels", {}).get(y, y) for y in ch["y"]]
    classes = [("s1" if i == 0 else "ctx") if ch.get("style") == "emphasis" else f"s{i + 1}"
               for i in range(len(ys))]
    k = len(ys)
    bar = 16 if k == 1 else 11
    band = (bar + 14) if k == 1 else (k * bar + (k - 1) * 2 + 14)
    L, R, T = 112, 56, 22
    n = len(rows)
    H = T + band * n + 6
    pw = W - L - R
    hi = max((r[j] for r in rows for j in ys if is_number(r[j])), default=0.0)
    ticks = nice_ticks(hi, 4)

    def X(v: float) -> float:
        return L + pw * max(v, 0) / ticks[-1]

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{esc(", ".join(names))} by {esc(ch["x"])}">']
    for t in ticks:
        out.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{T - 4}" y2="{H - 4}"/>')
        out.append(f'<text class="tick" x="{X(t):.1f}" y="{T - 9}" text-anchor="middle">{esc(compact(t, fmt))}</text>')
    for i, r in enumerate(rows):
        y0 = T + i * band + 7
        name = str(r[xi])
        short = name if len(name) <= 17 else name[:16] + "…"
        out.append(f'<text class="cat" x="{L - 10}" y="{y0 + (band - 14) / 2 + 4:.1f}" text-anchor="end">{esc(short)}</text>')
        for s, j in enumerate(ys):
            v = r[j] if is_number(r[j]) else 0.0
            yb = y0 + s * (bar + 2)
            w = X(v) - L
            if w > 0.5:
                out.append(f'<path class="bar {classes[s]}" d="{_bar_path(L, yb, w, bar)}"/>')
            if s == 0:                              # value at the tip, accent series only
                out.append(f'<text class="val" x="{L + w + 6:.1f}" y="{yb + bar / 2 + 4:.1f}">{esc(compact(v, fmt))}</text>')
        tip = _tip(name, [(full(r[j], fmt), nm, c) for j, nm, c in zip(ys, names, classes, strict=True)])
        out.append(f'<rect class="hit" tabindex="0" x="0" y="{y0 - 5:.1f}" width="{W}" height="{band:.1f}" data-tip="{tip}"/>')
    out.append(f'<line class="axis" x1="{L}" x2="{L}" y1="{T - 4}" y2="{H - 4}"/>')
    out.append("</svg>")
    return _legend(names, classes, "rect") + "".join(out)


def bar_chart(cols: list[str], rows: list[list[Any]], ch: dict[str, Any]) -> str:
    xi, ys = cols.index(ch["x"]), [cols.index(y) for y in ch["y"]]
    fmt = ch.get("format", "count")
    names = [ch.get("labels", {}).get(y, y) for y in ch["y"]]
    classes = [("s1" if i == 0 else "ctx") if ch.get("style") == "emphasis" else f"s{i + 1}"
               for i in range(len(ys))]
    k, n = len(ys), len(rows)
    L, R, T, B, H = 52, 12, 14, 30, 230
    pw, ph = W - L - R, H - T - B
    slot = pw / max(n, 1)
    bar = min(24.0, (slot * 0.6 - (k - 1) * 2) / k)
    hi = max((r[j] for r in rows for j in ys if is_number(r[j])), default=0.0)
    ticks = nice_ticks(hi)

    def Y(v: float) -> float:
        return T + ph - ph * max(v, 0) / ticks[-1]

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{esc(", ".join(names))} by {esc(ch["x"])}">']
    for t in ticks:
        out.append(f'<line class="grid" x1="{L}" x2="{L + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>')
        out.append(f'<text class="tick" x="{L - 8}" y="{Y(t) + 4:.1f}" text-anchor="end">{esc(compact(t, fmt))}</text>')
    for i, r in enumerate(rows):
        gx = L + slot * i + (slot - (k * bar + (k - 1) * 2)) / 2
        for s, j in enumerate(ys):
            v = r[j] if is_number(r[j]) else 0.0
            h = Y(0) - Y(v)
            if h > 0.5:
                out.append(f'<path class="bar {classes[s]}" d="{_bar_path(gx + s * (bar + 2), Y(v), bar, h, horizontal=False)}"/>')
        out.append(f'<text class="tick" x="{L + slot * (i + .5):.1f}" y="{H - 10}" text-anchor="middle">{esc(month_label(r[xi]) if _month(r[xi]) else r[xi])}</text>')
        tip = _tip(str(r[xi]), [(full(r[j], fmt), nm, c) for j, nm, c in zip(ys, names, classes, strict=True)])
        out.append(f'<rect class="hit" tabindex="0" x="{L + slot * i:.1f}" y="{T}" width="{slot:.1f}" height="{ph}" data-tip="{tip}"/>')
    out.append(f'<line class="axis" x1="{L}" x2="{L + pw}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    out.append("</svg>")
    return _legend(names, classes, "rect") + "".join(out)


CHARTS = {"line": line_chart, "hbar": hbar_chart, "bar": bar_chart}


def chart_html(q: dict[str, Any], ch: dict[str, Any]) -> str:
    cols, rows = q.get("columns", []), q.get("rows", [])
    missing = [c for c in [ch["x"], *ch["y"]] if c not in cols]
    if missing:
        return f'<p class="note">Chart not drawn: the query has no column {esc(", ".join(missing))}.</p>'
    if not rows:
        return '<p class="note">Chart not drawn: the query returned no rows.</p>'
    cap = f'<figcaption>{esc(ch["caption"])}</figcaption>' if ch.get("caption") else ""
    return f'<figure>{CHARTS[ch["type"]](cols, rows, ch)}{cap}</figure>'


def table_html(q: dict[str, Any], fmt_hint: dict[str, str] | None = None) -> str:
    cols, rows = q.get("columns", []), q.get("rows", [])
    if not rows:
        return ""
    fmts = {c: (fmt_hint or {}).get(c) or column_format(c) for c in cols}
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(
        f'<td class="{"num" if is_number(v) else ""}">{esc(full(v, fmts[c]) if is_number(v) else (month_label(v) if _month(v) else v))}</td>'
        for c, v in zip(cols, r, strict=True)) + "</tr>" for r in rows)
    return (f'<details class="tableview"><summary>Show the numbers ({len(rows)} row'
            f'{"s" if len(rows) != 1 else ""})</summary><div class="scroll"><table><thead><tr>{head}'
            f"</tr></thead><tbody>{body}</tbody></table></div></details>")


# --- page parts ---------------------------------------------------------------------------

def prose_html(text: str | None, failure: str | None) -> str:
    if text is None:
        return f'<p class="note">Not drafted: {esc(failure)}.</p>'
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join("<p>" + esc(p).replace(esc(MARK), f'<mark title="This number could not be traced to a query result">{esc(MARK)}</mark>') + "</p>"
                   for p in paras)


def tiles_html(sec: dict[str, Any], q: dict[str, Any]) -> str:
    cols, rows = q.get("columns", []), q.get("rows", [])
    if not rows:
        return '<p class="note">No headline row returned.</p>'
    row = dict(zip(cols, rows[0], strict=True))
    month = row.get("month")
    prior_lab = f"vs {month_label(shift_month(month, -1))}" if _month(month) else "vs last period"
    year_lab = f"vs {month_label(shift_month(month, -12))}" if _month(month) else "vs a year ago"
    out = []
    for t in sec["tiles"]:
        v = row.get(t["value"])
        deltas = []
        for col, lab in ((t.get("vs_prior"), prior_lab), (t.get("vs_year"), year_lab)):
            d = row.get(col) if col else None
            if not is_number(d):
                continue
            good = (d > 0) == t.get("up_is_good", True) if d != 0 else None
            cls = "flat" if good is None else ("good" if good else "bad")
            arrow = "▲" if d > 0 else ("▼" if d < 0 else "■")
            unit = " pts" if t.get("change_format") == "pts" else "%"
            word = "up" if d > 0 else ("down" if d < 0 else "flat")
            deltas.append(f'<li class="{cls}"><span aria-hidden="true">{arrow}</span>'
                          f'<span class="sr">{word}</span> {abs(d):,.1f}{unit} <span class="vs">{esc(lab)}</span></li>')
        out.append(f'<div class="tile"><div class="tlabel">{esc(t["label"])}</div>'
                   f'<div class="tvalue" title="{esc(full(v, t.get("format", "count")))}">'
                   f'{esc(compact(v, t.get("format", "count")) if is_number(v) else "–")}</div>'
                   f'<ul class="deltas">{"".join(deltas)}</ul></div>')
    return f'<div class="tiles">{"".join(out)}</div>'


def decisions_html(q: dict[str, Any]) -> str:
    cols, rows = q.get("columns", []), q.get("rows", [])
    if not rows:
        return '<p class="allclear"><span aria-hidden="true">✓</span> No rule flagged anything this month.</p>'
    items = []
    for r in rows:
        row = dict(zip(cols, r, strict=True))
        sev = str(row.get("severity") or "info").lower()
        label, icon = SEVERITY.get(sev, SEVERITY["info"])
        figs = [f'{esc(c.removesuffix("_pct").replace("_", " "))} '
                f'<span title="{esc(full(v, column_format(c)))}">{esc(compact(v, column_format(c)))}</span>'
                for c, v in row.items() if c not in ("severity", "area", "issue", "threshold")
                and is_number(v)]
        items.append(f'<li class="item {esc(sev)}"><span class="chip {esc(sev)}"><span aria-hidden="true">{icon}</span> {label}</span>'
                     f'<div><div class="area">{esc(row.get("area"))}</div><div class="issue">{esc(row.get("issue"))}</div>'
                     f'<div class="figs">{" · ".join(figs)}</div></div></li>')
    return f'<ul class="items">{"".join(items)}</ul>'


def status_html(state: dict[str, Any]) -> str:
    a = state.get("approval")
    if a == "approved":
        return f'<span class="status ok">Approved by {esc(state.get("decided_by"))}</span>'
    if a == "rejected":
        return '<span class="status bad">Rejected, not published</span>'
    if a and a.startswith("auto"):
        return '<span class="status warn">Published without review (--no-gate)</span>'
    return '<span class="status warn">Draft, awaiting approval</span>'


def render_html(state: dict[str, Any]) -> str:
    spec = state["spec"]
    queries = state.get("queries", {})
    sections = [(sec, checked_section(state, sec), queries.get(sec["id"], {})) for sec in spec["sections"]]
    checked = sum(cs["checked"] for _, cs, _ in sections)
    unverified = sum(len(cs["removed"]) for _, cs, _ in sections)
    month = next((dict(zip(q.get("columns", []), q["rows"][0], strict=True)).get("month")
                  for s, _, q in sections if s.get("kind") == "kpis" and q.get("rows")), None)
    period = month_label(month, long=True) if _month(month) else (state.get("generated_at") or "")[:10]

    lead, decide, cards = [], [], []
    for sec, cs, q in sections:
        if sec.get("kind") == "kpis":
            lead.append(tiles_html(sec, q) + f'<div class="summary">{prose_html(cs["text"], cs["failure"])}</div>')
        elif sec.get("kind") == "decisions":
            n = len(q.get("rows", []))
            decide.append(f'<section class="card decide" aria-labelledby="h-{esc(sec["id"])}">'
                          f'<h2 id="h-{esc(sec["id"])}">{esc(sec["heading"])} <span class="count">{n}</span></h2>'
                          f'{decisions_html(q)}<div class="prose">{prose_html(cs["text"], cs["failure"])}</div></section>')
        else:
            fmt_hint = {y: sec["chart"].get("format", "count") for y in sec["chart"]["y"]} if sec.get("chart") else None
            chart = chart_html(q, sec["chart"]) if sec.get("chart") else ""
            cards.append(f'<section class="card" aria-labelledby="h-{esc(sec["id"])}">'
                         f'<h2 id="h-{esc(sec["id"])}">{esc(sec["heading"])}</h2>{chart}'
                         f'<div class="prose">{prose_html(cs["text"], cs["failure"])}</div>{table_html(q, fmt_hint)}</section>')

    sources = []
    for sec, cs, _ in sections:
        used = [k for k in cs["used"] if is_number(cs["facts"].get(k))]
        if used:
            sources.append(f'<h3>{esc(sec["heading"])}</h3><table><tbody>' + "".join(
                f'<tr><td><code>{esc(k)}</code></td><td class="num">{esc(repr(cs["facts"][k]) if isinstance(cs["facts"][k], float) else cs["facts"][k])}</td></tr>'
                for k in used) + "</tbody></table>")
    budget = state.get("budget") or {}
    prov = [f'spec <code>{esc(state.get("spec_name", "?"))}</code> version <code>sha256:{esc((state.get("spec_version") or "?")[:12])}</code>',
            f'model <code>{esc(state.get("model_label") or state.get("provider") or "?")}</code>',
            f'cost ${float(budget.get("spent_usd") or 0.0):.4f} ({int(budget.get("llm_calls") or 0)} model calls, list-price estimate)',
            f'data <code>{esc(state.get("db_path", "?"))}</code> (read-only)',
            f'generated {esc(state.get("generated_at", "?"))}']
    if state.get("thread"):
        prov.append(f'thread <code>{esc(state["thread"])}</code>')
    warn = (f'<p class="banner"><span aria-hidden="true">▲</span> {unverified} number(s) could not be traced to a query '
            f'result and were replaced with <mark>{esc(MARK)}</mark>.</p>') if unverified else ""
    title = f"{spec['title']}, {period}" if period else spec["title"]
    return PAGE.format(
        title=esc(title), heading=esc(spec["title"]), period=esc(period),
        description=esc(spec.get("description") or ""), status=status_html(state),
        checked=checked, unverified=unverified, warn=warn, lead="".join(lead),
        decide="".join(decide), cards="".join(cards),
        sources="".join(sources) or "<p>No numbers used.</p>",
        provenance=" · ".join(prov), css=CSS, js=JS)


CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--ctx:#c3c2b7;
--good:#006300;--bad:#d03b3b;--critical:#d03b3b;--warning:#fab219;--info:#898781;--wash-critical:#fbeceb;--wash-warning:#fdf5e2}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--ctx:#5f5e59;--good:#0ca30c;--wash-critical:#2e1a1a;--wash-warning:#2e2714}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--ctx:#5f5e59;--good:#0ca30c;
--wash-critical:#2e1a1a;--wash-warning:#2e2714}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
header .eyebrow{color:var(--ink2);font-size:13px;letter-spacing:.02em}
header h1{font-size:30px;line-height:1.2;margin:4px 0 6px;font-weight:650}
header .desc{color:var(--ink2);margin:0 0 12px}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px;color:var(--ink2)}
.status{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid var(--ring);font-weight:600;color:var(--ink)}
.status.ok::before{content:"✓ ";color:var(--good)} .status.warn::before{content:"● ";color:var(--warning)}
.status.bad::before{content:"✕ ";color:var(--bad)}
.banner{background:var(--wash-warning);border-radius:8px;padding:10px 14px;margin:16px 0 0}
mark{background:var(--wash-warning);color:var(--ink);padding:0 3px;border-radius:3px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0 12px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px}
.tlabel{color:var(--ink2);font-size:13px}
.tvalue{font-size:32px;font-weight:650;line-height:1.15;margin:4px 0 6px}
.deltas{list-style:none;margin:0;padding:0;font-size:13px}
.deltas li{display:flex;gap:6px;align-items:baseline}
.deltas .good span[aria-hidden]{color:var(--good)} .deltas .bad span[aria-hidden]{color:var(--bad)}
.deltas .good{color:var(--ink)} .deltas .bad{color:var(--ink)} .deltas .vs{color:var(--muted)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.summary{font-size:16px;max-width:820px} .summary p{margin:6px 0}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:18px 20px;margin:16px 0}
.card h2{font-size:18px;margin:0 0 10px;font-weight:650}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(460px,100%),1fr));gap:0 16px;align-items:start}
.decide{border-left:4px solid var(--critical)}
.count{display:inline-block;min-width:22px;text-align:center;font-size:13px;border-radius:999px;background:var(--wash-critical);padding:0 7px;margin-left:4px}
.items{list-style:none;padding:0;margin:0 0 10px}
.item{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--grid)}
.item:first-child{border-top:0}
.chip{flex:0 0 auto;font-size:12px;font-weight:650;border-radius:6px;padding:2px 8px;min-width:84px}
.chip.critical{background:var(--wash-critical)} .chip.critical span{color:var(--critical)}
.chip.warning{background:var(--wash-warning)} .chip.warning span{color:#b07800}
.chip.info{background:var(--grid)}
.area{font-weight:650} .issue{color:var(--ink)} .figs{color:var(--ink2);font-size:13px;font-variant-numeric:tabular-nums}
.allclear{margin:0} .allclear span{color:var(--good)}
.prose p{margin:8px 0;max-width:780px}
.note{color:var(--muted);font-style:italic}
figure{margin:4px 0 8px}
figcaption{color:var(--muted);font-size:12px}
.legend{list-style:none;display:flex;flex-wrap:wrap;gap:16px;padding:0;margin:0 0 4px;font-size:13px;color:var(--ink2)}
.legend li{display:flex;align-items:center;gap:6px}
.key{display:inline-block}.key.line{width:16px;height:2px;border-radius:1px}.key.rect{width:10px;height:10px;border-radius:2px}
.key.s1{background:var(--s1)}.key.s2{background:var(--s2)}.key.s3{background:var(--s3)}.key.ctx{background:var(--ctx)}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg .grid{stroke:var(--grid);stroke-width:1} svg .axis{stroke:var(--axis);stroke-width:1}
svg text{font:11px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--muted);font-variant-numeric:tabular-nums}
svg .cat{fill:var(--ink2);font-size:12px} svg .val,svg .endlab{fill:var(--ink);font-size:12px;font-weight:600}
svg .endname{fill:var(--ink2);font-size:11px}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .line.s1{stroke:var(--s1)} svg .line.s2{stroke:var(--s2)} svg .line.s3{stroke:var(--s3)} svg .line.ctx{stroke:var(--ctx)}
svg .dot{stroke:var(--surface);stroke-width:2} svg .dot.s1,svg .bar.s1{fill:var(--s1)} svg .dot.s2,svg .bar.s2{fill:var(--s2)}
svg .dot.s3,svg .bar.s3{fill:var(--s3)} svg .dot.ctx,svg .bar.ctx{fill:var(--ctx)}
svg .hit{fill:transparent;outline:none;cursor:default} svg .hit:focus-visible{stroke:var(--s1);stroke-width:1}
svg .crosshair{stroke:var(--axis);stroke-width:1}
.tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);
border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);z-index:9;max-width:260px}
.tip .tt{color:var(--ink2);margin-bottom:4px} .tip .tr{display:flex;align-items:center;gap:8px}
.tip .tv{font-weight:650;font-variant-numeric:tabular-nums} .tip .tn{color:var(--ink2)}
.tableview{margin-top:6px} .tableview summary{cursor:pointer;color:var(--ink2);font-size:13px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;font-size:13px;margin:8px 0;min-width:50%}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--grid)} th{color:var(--ink2);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
details.sources{margin-top:24px} details.sources summary{cursor:pointer;font-weight:600}
details.sources h3{font-size:14px;margin:14px 0 2px}
footer{color:var(--muted);font-size:12px;margin-top:20px;border-top:1px solid var(--grid);padding-top:12px}
code{font:12px ui-monospace,Consolas,monospace}
@media print{body{background:#fff}.card,.tile{break-inside:avoid}.tip{display:none}}
"""

JS = """
(function(){var tip=document.createElement('div');tip.className='tip';tip.hidden=true;document.body.appendChild(tip);
function show(el,x,y){var d;try{d=JSON.parse(el.getAttribute('data-tip'))}catch(e){return}
tip.textContent='';var t=document.createElement('div');t.className='tt';t.textContent=d.t;tip.appendChild(t);
d.r.forEach(function(r){var row=document.createElement('div');row.className='tr';var k=document.createElement('span');
k.className='key line '+r[2];var v=document.createElement('span');v.className='tv';v.textContent=r[0];
var n=document.createElement('span');n.className='tn';n.textContent=r[1];row.appendChild(k);row.appendChild(v);
row.appendChild(n);tip.appendChild(row)});tip.hidden=false;
var w=tip.offsetWidth,h=tip.offsetHeight;tip.style.left=Math.min(x+14,window.innerWidth-w-8)+'px';
tip.style.top=Math.max(8,y-h-10)+'px';var svg=el.ownerSVGElement,c=svg&&svg.querySelector('.crosshair');
if(c&&el.hasAttribute('data-x')){c.setAttribute('x1',el.dataset.x);c.setAttribute('x2',el.dataset.x);c.setAttribute('visibility','visible')}}
function hide(el){tip.hidden=true;var svg=el.ownerSVGElement,c=svg&&svg.querySelector('.crosshair');if(c)c.setAttribute('visibility','hidden')}
document.querySelectorAll('[data-tip]').forEach(function(el){
el.addEventListener('pointermove',function(e){show(el,e.clientX,e.clientY)});
el.addEventListener('pointerleave',function(){hide(el)});
el.addEventListener('focus',function(){var b=el.getBoundingClientRect();show(el,b.left+b.width/2,b.top)});
el.addEventListener('blur',function(){hide(el)})})})();
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body><div class="wrap">
<header><div class="eyebrow">{heading}</div><h1>{period}</h1><p class="desc">{description}</p>
<div class="meta">{status}<span>{checked} numbers checked against the queries</span><span>·</span><span>{unverified} unverified</span></div>{warn}</header>
<main>{lead}{decide}<div class="grid2">{cards}</div>
<details class="sources"><summary>Sources: every number in the text, traced to its query cell</summary>
<p>Each number in the text matched (within 0.5%) a cell returned by that section's read-only SQL query. Tiles, charts and tables are drawn by code from the same rows.</p>{sources}</details>
</main>
<footer>{provenance}<br>Generated by agentic-analytics: the model writes the prose; code draws every number.</footer>
</div><script>{js}</script></body></html>
"""
