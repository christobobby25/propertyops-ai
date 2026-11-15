"""Build the PropertyOps ADK agent from settings and injected dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

from app.agent.clock import Clock, SystemClock
from app.agent.guards import make_before_model_callback, make_before_tool_callback
from app.agent.identity import IdentityProvider, identity_provider_for
from app.agent.prompt import instruction_provider
from app.agent.tools import ToolDeps, build_tools
from app.agent.work_orders import WorkOrderRepository
from app.config import Settings, get_settings
from app.retrieval import PolicyRetriever

AGENT_NAME = "propertyops_assistant"


def build_agent(
    settings: Settings,
    identity: IdentityProvider,
    retriever_factory: Callable[[], PolicyRetriever],
    work_orders_factory: Callable[[], WorkOrderRepository],
    proposals_factory: Callable[[], object] | None = None,
) -> LlmAgent:
    """``proposals_factory`` enables the Stage 2 creation tools; omit it for read-only."""
    deps = ToolDeps(
        identity=identity,
        retriever_factory=retriever_factory,
        work_orders_factory=work_orders_factory,
        timeout_seconds=settings.tool_timeout_seconds,
        proposals_factory=proposals_factory,
    )
    fns = build_tools(deps)
    if proposals_factory is not None:
        from app.agent.creation_tools import build_creation_tools

        fns += build_creation_tools(deps)
    return LlmAgent(
        name=AGENT_NAME,
        model=settings.model,
        description="Answers maintenance-policy and work-order questions for the signed-in user.",
        instruction=instruction_provider(creation_enabled=proposals_factory is not None),
        tools=[FunctionTool(fn) for fn in fns],
        before_tool_callback=make_before_tool_callback(settings.max_tool_calls),
        before_model_callback=make_before_model_callback(settings.max_llm_calls),
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            http_options=types.HttpOptions(timeout=int(settings.model_timeout_seconds * 1000)),
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


@dataclass
class Factories:
    retriever: Callable[[], PolicyRetriever]
    work_orders: Callable[[], WorkOrderRepository]
    proposals: Callable[[], object]


def firestore_factories(settings: Settings, clock: Clock | None = None) -> Factories:
    """Lazy dependencies sharing ONE Firestore client for the configured target
    (cloud or emulator — see ``app.gcp.firestore_client``; no fallback)."""

    @cache
    def db():
        from app.gcp import firestore_client

        return firestore_client(settings)

    def retriever() -> PolicyRetriever:
        from app.embeddings import VertexEmbedder
        from app.retrieval import FirestorePolicyStore

        return PolicyRetriever(FirestorePolicyStore(db()), VertexEmbedder(settings))

    def work_orders() -> WorkOrderRepository:
        from app.agent.work_orders import FirestoreWorkOrderRepository

        return FirestoreWorkOrderRepository(db(), timeout=settings.tool_timeout_seconds)

    def proposals():
        from app.agent.proposals import FirestoreProposalRepository, ProposalService
        from app.retrieval import FirestorePolicyStore

        return ProposalService(
            FirestoreProposalRepository(db()),
            work_orders(),
            FirestorePolicyStore(db()),
            clock or SystemClock(),
            settings.proposal_ttl_seconds,
        )

    return Factories(retriever, work_orders, proposals)


def cloud_factories(settings: Settings):
    """Read-only factories (retriever, work_orders) — kept for Stage 1 callers."""
    f = firestore_factories(settings)
    return f.retriever, f.work_orders


def build_default_agent() -> LlmAgent:
    """Agent for ADK Web / CLI: dev fixture identity in development, else fail closed.
    Creation tools are registered only when ENABLE_WORK_ORDER_CREATION=true."""
    s = get_settings()
    f = firestore_factories(s)
    return build_agent(
        s,
        identity_provider_for(s, get_settings),
        f.retriever,
        f.work_orders,
        f.proposals if s.enable_work_order_creation else None,
    )
