# ADR-0002: A deterministic checker, not an LLM, decides whether a number is supported

**Status:** accepted · 2026-09-30

## Context
Asking the model to cite its numbers helps, but a citation is just more model output. Asking
a second model to judge the first one moves the problem instead of solving it. The check
must be something an auditor can re-run and get the same answer.

## Decision
`check.py` is regex plus arithmetic. No LLM, no network.

* Every number in a section's text must match a fact of **that section's** query within
  0.5% relative tolerance (1e-9 absolute for zero). Otherwise it is *unsupported*.
* `12%` matches a fact of 12 or 0.12. `1,234.5` is parsed with separators. Signs count.
* Ignored: identifiers where digits touch letters or hyphens (`SEV1`, `g3-v2`), list markers
  at the start of a line, inline fact keys (`revenue[0]`), and dates or 4-digit years **only
  if** that year appears in a date-like cell of the query result.
* The model's own `numbers_used` citations are verified separately and reported as
  `bad_citations`; they never make a number supported.

The graph (`graph.py`) runs the check after drafting. Failing sections get **one** repair
call that quotes the unsupported numbers back. Whatever is still unsupported after that is
shown at the human gate and, if approved, is replaced by `[unverified]` in the published
report (`render.py`). The unverified values themselves are not printed on the page.

The Sources block under the report lists every fact the text used, derived by the checker,
not by the model.

## Consequences
* A false positive (a true number flagged) costs a repair call or an `[unverified]` mark. A
  false negative (an invented number that happens to be within 0.5% of some fact in the same
  section) is possible and is the known limit of this approach.
* Derived numbers the model computes itself ("a gap of 3,829") are rejected by design. If a
  report needs a derived number, the spec's SQL computes it.
* Tests: `tests/test_check.py`; the eval's `fabricate-once` / `fabricate-always` runs prove
  the repair and `[unverified]` paths (`eval/report_eval.py`).
