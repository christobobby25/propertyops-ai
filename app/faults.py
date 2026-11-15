"""Development-only fault injection for load and failure testing.

    FAULT_INJECTION="work_orders_timeout=0.3,work_orders_error=0.1,retrieval_error=0.2"

Each value is a probability per backend call. Timeouts sleep past the tool
timeout so the real timeout path runs. Refused unless APP_ENV=development.
"""

from __future__ import annotations

import random
import time

KINDS = ("work_orders_timeout", "work_orders_error", "retrieval_timeout", "retrieval_error")


class FaultConfigError(ValueError):
    pass


def parse(spec: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        key, _, value = part.partition("=")
        if key not in KINDS:
            raise FaultConfigError(f"unknown fault {key!r}; known: {', '.join(KINDS)}")
        try:
            p = float(value)
        except ValueError:
            raise FaultConfigError(f"{key}: probability must be a number") from None
        if not 0 <= p <= 1:
            raise FaultConfigError(f"{key}: probability must be between 0 and 1")
        out[key] = p
    return out


class FaultyWorkOrders:
    def __init__(self, inner, faults: dict[str, float], timeout_s: float, rng=random.random):
        self.inner, self.faults, self.timeout_s, self.rng = inner, faults, timeout_s, rng

    def list_for_units(self, unit_ids):
        if self.rng() < self.faults.get("work_orders_timeout", 0):
            time.sleep(self.timeout_s + 0.5)
        if self.rng() < self.faults.get("work_orders_error", 0):
            raise ConnectionError("injected work-order backend error")
        return self.inner.list_for_units(unit_ids)


class FaultyRetriever:
    def __init__(self, inner, faults: dict[str, float], timeout_s: float, rng=random.random):
        self.inner, self.faults, self.timeout_s, self.rng = inner, faults, timeout_s, rng

    def search(self, auth, query, **kw):
        if self.rng() < self.faults.get("retrieval_timeout", 0):
            time.sleep(self.timeout_s + 0.5)
        if self.rng() < self.faults.get("retrieval_error", 0):
            raise ConnectionError("injected retrieval error")
        return self.inner.search(auth, query, **kw)
