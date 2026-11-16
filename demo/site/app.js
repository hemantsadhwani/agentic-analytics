// The demo page: send a question, show the checked answer and its working.
// Everything that comes back is inserted with textContent, never as HTML.
(function () {
  "use strict";
  var form = document.getElementById("ask-form");
  var input = document.getElementById("q");
  var btn = document.getElementById("ask-btn");
  var box = document.getElementById("answer");
  var budget = document.getElementById("budget");
  var STATUS = { answered: "Answered", clarify: "Asks back", refused: "Refused",
                 not_understood: "Not found", error: "Error" };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function num(v) {
    if (typeof v !== "number") return v === null || v === undefined ? "" : String(v);
    return Number.isInteger(v) ? v.toLocaleString("en-US")
      : v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function health() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      if (!h.enabled) { budget.textContent = "The demo is paused by its owner."; return; }
      var t = h.today || {};
      if (typeof t.calls === "number") {
        budget.textContent = "Today: " + t.calls + " of " + t.call_cap + " questions used. " +
          'Data runs from Dec 2010 to Jan 2014, so "last month" means the latest month in the data.';
      }
    }).catch(function () {});
  }

  function ask(question, metric) {
    btn.disabled = true;
    box.hidden = false;
    box.textContent = "";
    var w = el("p", "route"); w.appendChild(el("span", "spin")); w.appendChild(document.createTextNode(" Working: linking the schema, writing one read-only query, checking the numbers…"));
    box.appendChild(w);
    var body = { question: question };
    if (metric) body.metric = metric;
    fetch("/api/ask", { method: "POST", headers: { "content-type": "application/json" },
                        body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { code: r.status, j: j }; }); })
      .then(function (res) { show(question, res.code, res.j); })
      .catch(function () { show(question, 0, { error: "network", message: "Could not reach the demo. Try again in a moment." }); })
      .finally(function () { btn.disabled = false; health(); });
  }

  function show(question, code, a) {
    box.textContent = "";
    if (code !== 200) {
      var h = el("div", "head"); h.appendChild(el("span", "pill error", code === 429 ? "Budget" : "Unavailable"));
      box.appendChild(h);
      box.appendChild(el("p", "atext", a.message || "Something went wrong."));
      return;
    }
    var head = el("div", "head");
    head.appendChild(el("span", "pill " + a.status, STATUS[a.status] || a.status));
    if (a.route && a.route !== "none") head.appendChild(el("span", "route", a.route === "sql" ? "from the warehouse (SQL)" : "from product documents"));
    box.appendChild(head);
    box.appendChild(el("p", "qtext", question));
    box.appendChild(el("p", "atext", a.text));

    if (a.status === "clarify" && a.options && a.options.length) {
      var opts = el("div", "options");
      a.options.forEach(function (o) {
        var b = el("button", "btn", o.label); b.type = "button";
        b.addEventListener("click", function () { ask(question, o.metric); });
        opts.appendChild(b);
      });
      box.appendChild(opts);
    }

    if (a.rows && a.rows.length > 1 && a.columns && a.columns.length) {
      var wrap = el("div", "scroll"), t = el("table"), thead = el("thead"), tr = el("tr");
      a.columns.forEach(function (c) { tr.appendChild(el("th", null, c)); });
      thead.appendChild(tr); t.appendChild(thead);
      var tb = el("tbody");
      a.rows.forEach(function (r) {
        var row = el("tr");
        r.forEach(function (v) { row.appendChild(el("td", typeof v === "number" ? "num" : null, num(v))); });
        tb.appendChild(row);
      });
      t.appendChild(tb); wrap.appendChild(t); box.appendChild(wrap);
      if (a.rows_truncated) box.appendChild(el("p", "hint", "First 50 rows shown."));
    }

    if (a.citations && a.citations.length) {
      var cs = el("ul", "list");
      a.citations.forEach(function (c) { cs.appendChild(el("li", null, "Source: " + c.title)); });
      box.appendChild(cs);
    }

    var work = el("details"); work.appendChild(el("summary", null, "Show the work"));
    if (a.sql) {
      work.appendChild(el("p", "route", "The one read-only query" + (a.sql_repaired ? " (repaired once: " + (a.repair_reasons || []).join("; ") + ")" : "") + ":"));
      work.appendChild(el("pre", null, a.sql));
    }
    if (a.assumptions && a.assumptions.length) {
      work.appendChild(el("p", "route", "Assumptions:"));
      var ul = el("ul", "list");
      a.assumptions.forEach(function (s) { ul.appendChild(el("li", null, s)); });
      work.appendChild(ul);
    }
    if (a.tables && a.tables.length) work.appendChild(el("p", "route", "Tables linked: " + a.tables.join(", ")));
    if (a.check) {
      var n = a.check.numbers_checked;
      work.appendChild(el("p", "route", a.check.ok ? "Number check: " + (n || 0) + " number(s), all traced to the query or the cited text."
        : "Number check failed: " + (a.check.unsupported || []).join(", ")));
    }
    if (a.data_as_of) work.appendChild(el("p", "route", "Data as of " + a.data_as_of + "."));
    box.appendChild(work);

    var meta = el("div", "meta");
    if (a.model) meta.appendChild(el("span", null, "Model: " + a.model));
    if (a.input_tokens || a.output_tokens) meta.appendChild(el("span", null, "Tokens: " + a.input_tokens + " in / " + a.output_tokens + " out"));
    meta.appendChild(el("span", null, "Cost: $" + (a.cost_usd || 0).toFixed(6)));
    if (a.latency_ms) meta.appendChild(el("span", null, "Time: " + a.latency_ms + " ms"));
    if (!a.model) meta.appendChild(el("span", null, "No model call: decided in code"));
    box.appendChild(meta);
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (q) ask(q);
  });
  document.querySelectorAll("#chips button").forEach(function (b) {
    b.addEventListener("click", function () {
      input.value = b.getAttribute("data-q");
      ask(input.value);
    });
  });
  health();
  // A shareable link can carry a question: /?q=Which+road+frame+is+the+lightest%3F
  var preset = new URLSearchParams(window.location.search).get("q");
  if (preset && preset.length <= 300) { input.value = preset; ask(preset); }
})();
