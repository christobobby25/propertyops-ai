"""Offline fakes for agent tests: seed-data-backed repositories, no cloud."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app.agent.identity import StaticIdentityProvider
from app.agent.tools import ToolDeps, build_tools
from app.demo_auth import load_demo_context
from app.policy_docs import build_chunks, load_policy_documents
from app.retrieval import PolicyRetriever
from app.seed_data import SEED_DIR

SEED_WORK_ORDERS = json.loads((SEED_DIR / "work_orders.json").read_text())
SEED_UNITS = {u["unit_id"]: u for u in json.loads((SEED_DIR / "units.json").read_text())}


class SeedWorkOrderRepo:
    """Deliberately over-fetches (returns EVERY work order) so tests prove the
    access rules — not the query — are what enforce scope."""

    def __init__(self):
        self.calls = []

    def list_for_units(self, unit_ids):
        self.calls.append(list(unit_ids))
        return [
            {**wo, "unit_number": SEED_UNITS[wo["unit_id"]]["unit_number"]}
            for wo in SEED_WORK_ORDERS
        ]


class SeedPolicyStore:
    """Returns chunks for the requested property (like Firestore's pre-filter)."""

    def __init__(self, leak_all_properties: bool = False):
        docs = load_policy_documents()
        self.chunks = build_chunks(docs, {d.document_id: f"gs://t/{d.filename}" for d in docs})
        self.leak = leak_all_properties
        self.calls = []

    def find_nearest(self, *, property_id, query_vector, limit):
        self.calls.append(property_id)
        rows = [
            {**c.__dict__, "audience": list(c.audience), "vector_distance": 0.1}
            for c in self.chunks
            if self.leak or c.property_id == property_id
        ]
        return rows[:limit] if not self.leak else rows


class FakeEmbedder:
    dimensions = 3

    def embed(self, texts, task_type):
        return [[0.0, 0.0, 1.0] for _ in texts]


class Failing:
    def __init__(self, exc: BaseException):
        self.exc = exc

    def list_for_units(self, unit_ids):
        raise self.exc

    def search(self, auth, query, **kw):
        raise self.exc


class Slow:
    def __init__(self, seconds: float):
        self.seconds = seconds

    def list_for_units(self, unit_ids):
        time.sleep(self.seconds)
        return []

    def search(self, auth, query, **kw):
        time.sleep(self.seconds)
        return []


def make_tools(
    context_id: str | None = "AUTH-RES-001",
    *,
    retriever=None,
    repo=None,
    timeout: float = 2.0,
    identity=None,
):
    auth = load_demo_context(context_id) if context_id else None
    retriever = retriever or PolicyRetriever(SeedPolicyStore(), FakeEmbedder())
    repo = repo or SeedWorkOrderRepo()
    deps = ToolDeps(
        identity=identity or StaticIdentityProvider(auth),
        retriever_factory=lambda: retriever,
        work_orders_factory=lambda: repo,
        timeout_seconds=timeout,
    )
    search, list_wos = build_tools(deps)
    return SimpleNamespace(search=search, list_wos=list_wos, repo=repo, retriever=retriever)


TOOL_CONTEXT = SimpleNamespace(state={})
