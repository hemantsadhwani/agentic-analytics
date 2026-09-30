"""A spend / call budget for the drafting calls (stdlib only).

Bounded autonomy: a report run may not spend more than the operator allows.

* ``max_usd`` caps the estimated (list-price) USD across all LLM calls in a run;
* ``max_llm_calls`` caps the number of successful model responses.

Either limit may be ``None`` (unbounded). ``draft.py`` checks ``denied()`` *before* each
section's call and ``charge()``s the response cost *after* it. Once exhausted, the
remaining sections are recorded as "not drafted" and the run goes on to the human gate
with ``stop_reason="budget"``, so a person sees exactly what was skipped.

Enforcement is pre-call, so a run can overshoot a USD cap by at most one call's cost.
Only responses are charged: a provider exception carries no cost figure.

The graph state is plain JSON (it is checkpointed), so the budget lives in it as a dict;
``denied`` / ``charge`` below rebuild a ``Budget`` from that dict and write it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INF = "∞"


@dataclass
class Budget:
    max_usd: float | None = None
    max_llm_calls: int | None = None
    spent_usd: float = 0.0
    llm_calls: int = 0

    def exhausted(self) -> bool:
        return self.reason() is not None

    def reason(self) -> str | None:
        """Which limit is exhausted, as ``"$0.2000 ≥ $0.1500"`` / ``"1 ≥ 1 llm calls"``."""
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            return f"${self.spent_usd:.4f} ≥ ${self.max_usd:.4f}"
        if self.max_llm_calls is not None and self.llm_calls >= self.max_llm_calls:
            return f"{self.llm_calls} ≥ {self.max_llm_calls} llm calls"
        return None

    def charge(self, cost_usd: float) -> None:
        """Record one completed LLM call and its (estimated) cost."""
        self.llm_calls += 1
        self.spent_usd += float(cost_usd or 0.0)

    @property
    def bounded(self) -> bool:
        return self.max_usd is not None or self.max_llm_calls is not None

    # ------------------------------------------------------------ (de)serialise
    def to_dict(self) -> dict[str, Any]:
        return {"max_usd": self.max_usd, "max_llm_calls": self.max_llm_calls,
                "spent_usd": self.spent_usd, "llm_calls": self.llm_calls}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Budget:
        d = d or {}
        return cls(max_usd=d.get("max_usd"), max_llm_calls=d.get("max_llm_calls"),
                   spent_usd=float(d.get("spent_usd") or 0.0),
                   llm_calls=int(d.get("llm_calls") or 0))

    def footer(self, stop_reason: str | None) -> str:
        """``budget: $0.0000/∞ · llm calls 0/∞ · stopped: published``."""
        max_usd = INF if self.max_usd is None else f"${self.max_usd:.4f}"
        max_calls = INF if self.max_llm_calls is None else str(self.max_llm_calls)
        stopped = stop_reason or "completed"
        if stopped == "budget" and self.reason():
            stopped = f"budget ({self.reason()})"
        return (f"budget: ${self.spent_usd:.4f}/{max_usd} · "
                f"llm calls {self.llm_calls}/{max_calls} · stopped: {stopped}")


# ------------------------------------------------------------ helpers over the JSON state
def denied(budget: dict[str, Any]) -> str | None:
    """Why the next call is not allowed, or ``None`` if it is."""
    return Budget.from_dict(budget).reason()


def charge(budget: dict[str, Any], cost_usd: float | None) -> None:
    """Charge one completed call, writing the new totals back into ``budget`` in place."""
    b = Budget.from_dict(budget)
    b.charge(cost_usd or 0.0)
    budget.update(b.to_dict())


def footer(budget: dict[str, Any], stop_reason: str | None) -> str:
    """``budget: $0.0000/∞ · llm calls 4/∞ · stopped: published``."""
    return Budget.from_dict(budget).footer(stop_reason or "completed")
