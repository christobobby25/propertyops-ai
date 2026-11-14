"""Build the PropertyOps ADK agent from settings and injected dependencies."""

from __future__ import annotations

from collections.abc import Callable

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

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
) -> LlmAgent:
    deps = ToolDeps(
        identity=identity,
        retriever_factory=retriever_factory,
        work_orders_factory=work_orders_factory,
        timeout_seconds=settings.tool_timeout_seconds,
    )
    return LlmAgent(
        name=AGENT_NAME,
        model=settings.model,
        description="Answers maintenance-policy and work-order questions for the signed-in user.",
        instruction=instruction_provider,
        tools=[FunctionTool(fn) for fn in build_tools(deps)],
        before_tool_callback=make_before_tool_callback(settings.max_tool_calls),
        before_model_callback=make_before_model_callback(settings.max_llm_calls),
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            http_options=types.HttpOptions(timeout=int(settings.model_timeout_seconds * 1000)),
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def cloud_factories(settings: Settings):
    """Lazy Firestore/Vertex-backed dependencies (created on first tool use)."""

    def retriever() -> PolicyRetriever:
        from app.embeddings import VertexEmbedder
        from app.gcp import firestore_client
        from app.retrieval import FirestorePolicyStore

        return PolicyRetriever(
            FirestorePolicyStore(firestore_client(settings)), VertexEmbedder(settings)
        )

    def work_orders() -> WorkOrderRepository:
        from app.agent.work_orders import FirestoreWorkOrderRepository
        from app.gcp import firestore_client

        return FirestoreWorkOrderRepository(
            firestore_client(settings), timeout=settings.tool_timeout_seconds
        )

    return retriever, work_orders


def build_default_agent() -> LlmAgent:
    """Agent for ADK Web / CLI: dev fixture identity in development, else fail closed."""
    s = get_settings()
    retriever, work_orders = cloud_factories(s)
    return build_agent(s, identity_provider_for(s, get_settings), retriever, work_orders)
