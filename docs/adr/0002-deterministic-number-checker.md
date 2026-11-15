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

## Amendment, 2026-11-12: checker v2 (probes and mutation testing)

A self-audit ran hand-written sentences against v1 and found escapes and false positives.
Every probe is now a regression test (`tests/test_check_probes.py`).

* **Escapes closed.** Scaled numbers (`112k`, `1.2M`, `0.11 million`, `1.1 lakh`, `2 crore`),
  multipliers (`3x`), units (`250ms`), number words (`one hundred and sixty`,
  `twelve thousand`, `three times`) and impossible dates (`2026-13`) are now checked. Small
  counts under twenty in words stay allowed, because the prompt asks for them.
* **False positives fixed.** Direction words set the sign (`fell 9.8%` = -9.8, and `rose 9.8%`
  against -9.8 now fails). Indian grouping (`1,11,850.2`) parses. Percentages and scaled
  numbers may be the fact rounded to the written precision (`-10%` for -9.8), capped at 5% of
  the fact so a coarse token cannot borrow another fact (`1k` is not 699).
* **Citations enforced.** With the model's `numbers_used`, every number must be cited by a
  key whose value it matches, and every citation must match its fact. Uncited numbers and
  bad citations fail the section and go to the repair call. Before v2 they were recorded
  and ignored.
* **Attribution hints.** A true number whose cited column, or (multi-row results) whose row
  label, is not named in its sentence produces a warning shown at the gate. It is a hint, not
  proof: meaning stays the reviewer's job.
* **Mutation test.** `tests/test_check_mutation.py` corrupts every number of an honest section
  53 ways (scale, digit swap, sign or direction flip, `k` suffix, number words). Every mutant
  more than 0.5% from the truth is rejected; the ones within 0.5% are equivalent by the
  tolerance rule and counted, not hidden.

Still not caught, by design: `twice`, `half`, `doubled`, `a third`; a true number in a false
sentence when its column and row are both named.
