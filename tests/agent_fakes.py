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


# --- Stage 2 fakes ----------------------------------------------------------------


class SeedSectionCatalog:
    def __init__(self):
        self.chunks = {c.chunk_id: c for c in SeedPolicyStore().chunks}

    def published_sections(self, property_id, section_ids):
        return {
            s
            for s in section_ids
            if s in self.chunks
            and self.chunks[s].property_id == property_id
            and self.chunks[s].status == "published"
        }


class RepoWorkOrders:
    """Work-order reads backed by the in-memory proposal repo (sees new orders)."""

    def __init__(self, repo):
        self.repo = repo

    def list_for_units(self, unit_ids):
        return [
            {**wo, "unit_number": SEED_UNITS[wo["unit_id"]]["unit_number"]}
            for wo in self.repo.work_orders.values()
            if wo["unit_id"] in unit_ids
        ]


class FakeToolContext:
    """Mimics the parts of ADK's ToolContext the creation tools use."""

    _n = 0

    def __init__(self, confirmation=None, function_call_id=None):
        FakeToolContext._n += 1
        self.function_call_id = function_call_id or f"call-{FakeToolContext._n}"
        self.tool_confirmation = confirmation
        self.requested = None
        self.state = {}

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested = {"hint": hint, "payload": payload}


def confirmed(yes=True):
    from google.adk.tools.tool_confirmation import ToolConfirmation

    return ToolConfirmation(confirmed=yes)


SNAPSHOT = "2026-11-13T12:00:00-06:00"


def make_service(ttl=900, clock=None):
    from datetime import datetime

    from app.agent.clock import FixedClock
    from app.agent.proposals import InMemoryProposalRepository, ProposalService

    repo = InMemoryProposalRepository(SEED_WORK_ORDERS)
    clock = clock or FixedClock(datetime.fromisoformat(SNAPSHOT))
    svc = ProposalService(repo, RepoWorkOrders(repo), SeedSectionCatalog(), clock, ttl)
    return svc, repo, clock


def make_creation_tools(context_id="AUTH-RES-002", *, service=None, timeout=2.0, identity=None):
    from app.agent.creation_tools import build_creation_tools

    svc = service or make_service()[0]
    auth = load_demo_context(context_id) if context_id else None
    deps = ToolDeps(
        identity=identity or StaticIdentityProvider(auth),
        retriever_factory=lambda: PolicyRetriever(SeedPolicyStore(), FakeEmbedder()),
        work_orders_factory=lambda: svc.work_orders,
        timeout_seconds=timeout,
        proposals_factory=lambda: svc,
    )
    prepare, submit, cancel, mine = build_creation_tools(deps)
    return SimpleNamespace(prepare=prepare, submit=submit, cancel=cancel, mine=mine, svc=svc)
