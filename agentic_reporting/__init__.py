"""Agentic report generation from read-only SQL facts.

Agents draft a report section by section from the rows a read-only SQL query returned.
A deterministic checker (``check.py``) rejects any number the model did not get from a
query. A human approves at a LangGraph ``interrupt()`` before anything is published, and an
``audit`` row records the decision (``audit.py``). Golden reports gate it in CI
(``python -m eval.report_eval --fake``). The same pipeline runs against a local Ollama
model with no other network access (``tests/test_private_mode.py``).

Entry point: ``python -m agentic_reporting.run --spec specs/sales_mbr.yaml --db samples/sales.sqlite``.
"""

__version__ = "0.1.0"
