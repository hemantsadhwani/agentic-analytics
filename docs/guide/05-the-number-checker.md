# 5. The number checker

## Why plain code, not another AI?

A popular idea is "LLM as a judge": ask a second model whether the first one was right. It
helps for style and tone. For numbers it has two problems:

* It can be wrong too, and in the same confident way.
* It may give a different verdict on the same text tomorrow.

A check that decides whether a number reaches a board must be **deterministic**: same input,
same answer, every time, and explainable line by line. So `agentic_reporting/check.py` is
regular expressions and arithmetic. No model is involved, ever. That rule is written in the
repo's instructions.

## What it does, in one paragraph

It finds every number in the text, works out what value it states (including its sign and
scale), and looks for a fact from that section's query that matches it within a small
tolerance. A number with no matching fact is **unsupported**, and the section fails. The model's
citations are checked too.

## The tricky part: numbers are written in many ways

People, and models, do not write `111850.2`. They write:

| Written | Means | Handled as |
|---|---|---|
| `111,850` or `1,11,850.2` | the same number, US or Indian grouping | both parsed |
| `112k`, `1.1 lakh`, `2.3 crore`, `3x` | scaled numbers | value times the scale |
| `twelve`, `three times` | number words | checked like digits (a bare "one", as in "one of the regions", is skipped) |
| `fell 9.8%` (fact stored as `-9.8`) | the sign comes from the word | "fell", "down", "decline" make it negative |
| `29 September 2026`, `2026-09-29` | dates | allowed only if the year is in the data and the date is real |
| `the top five products` (five rows came back) | a count of rows | the row count is a fact |

Each row of that table was once a bug: either a real number that escaped checking, or a
correct number wrongly rejected. We found them by attacking our own checker on purpose, then
pinned each case as a test (`tests/test_check_probes.py`).

## Decision: how close is close enough?

A model may round: `111,850` for `111,850.2`. That is fine. It may also "round" `699` to `1k`,
which is not fine.

**What we do.** A number passes if it is within 0.5% of a fact, or within the rounding its own
written precision allows ("112k" allows ±500), but that rounding allowance is capped at 5% of
the fact. So `1k` cannot match 699.

## Decision: citations are enforced

The model lists each number it used and its fact key. Two failures count:

* a number in the text that the model did not cite (**uncited**);
* a citation whose value does not match the fact it names (**bad citation**).

Both fail the section and trigger the repair. This turns "trust me" into "show me".

## Decision: warn when a true number may be attached to the wrong thing

The hardest error is a real number used for the wrong claim: last month's revenue presented
as this month's. Every number matches a fact, so nothing is "invented".

**What we do.** If a number's column ("revenue") or row label ("North") is not named in the
same sentence, the checker raises an **attribution warning**. It does not fail the section; it
is shown to the person at the gate. That is the honest division of labour: code catches
invented numbers; people judge meaning.

## Decision: digits inside a name are part of the name

A product called "Mountain-200 Black, 38" contains 200 and 38. They are not claims. The
checker masks names that come **from the data** (text cells of the result, and values the
chatbot resolved) before looking for numbers. Names the model invents are never masked, so a
model cannot hide a number by calling it a name.

## How do we know the checker itself works?

**Mutation testing.** A script takes correct sentences, corrupts each number in 53 different
ways (shift it, scale it, flip its sign, swap a digit...), and runs the checker on each. Every
corruption more than 0.5% away must be rejected. Result: 46 of 46 rejected; the other 7 are
within the tolerance by design and are counted as equivalent.

This is how you test a safety check: not only "does it pass good text?" but "does it fail every
kind of bad text?".

**Where.** `tests/test_check_mutation.py`, `tests/test_check_probes.py`, `tests/test_check.py`.

## What it does not catch (say this before anyone asks)

* Relative words: "twice", "half", "doubled".
* An invented number within 0.5% of a real fact.
* A false sentence whose row and column are both named correctly ("North led" when South did,
  with North's real number).

Meaning stays with the human reviewer. The checker guarantees one thing well: every number
traces to a cell of the query.

**In an enterprise.** Keep the deterministic check as the gate. You may add a model-based
reviewer for tone or meaning as an extra signal, but never as the thing that lets a number
through.

Next: [6. The chatbot](06-the-chatbot.md)
