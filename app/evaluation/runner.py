"""Run one evaluation case against the real agent (live model + Firestore)."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agent.factory import build_agent, cloud_factories
from app.agent.identity import StaticIdentityProvider
from app.config import Settings
from app.demo_auth import load_demo_context
from app.evaluation.cases import TOOL_ALIASES, EvalCase

APP_NAME = "propertyops_eval"


@dataclass
class CaseRun:
    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # {"name", "args"}
    tool_responses: list[dict] = field(default_factory=list)  # {"name", "response"}
    error: str | None = None
    seconds: float = 0.0


# --- Fault injection (harness-side wrappers; the agent is unchanged) --------


class _SlowRepo:
    def __init__(self, seconds: float):
        self.seconds = seconds

    def list_for_units(self, unit_ids):
        time.sleep(self.seconds)
        raise TimeoutError("injected")


class _InjectingStore:
    """Appends untrusted text to retrieved chunks of one document."""

    def __init__(self, inner, document_id: str, text: str):
        self.inner, self.document_id, self.text = inner, document_id, text

    def find_nearest(self, **kw):
        rows = self.inner.find_nearest(**kw)
        return [
            {**r, "text": f"{r['text']} {self.text}"}
            if r.get("document_id") == self.document_id
            else r
            for r in rows
        ]


def _factories(settings: Settings, case: EvalCase):
    retriever_factory, repo_factory = cloud_factories(settings)

    fault = case.setup.get("tool_fault")
    if fault and TOOL_ALIASES.get(fault["tool"], fault["tool"]) == "list_my_work_orders":
        if fault.get("behavior") == "timeout":
            delay = settings.tool_timeout_seconds + 2
            repo_factory = lambda: _SlowRepo(delay)  # noqa: E731

    rfault = case.setup.get("retrieval_fault")
    if rfault:
        base = retriever_factory

        def retriever_factory():
            r = base()
            r._store = _InjectingStore(
                r._store, rfault["document_id"], rfault["append_untrusted_text"]
            )
            return r

    return retriever_factory, repo_factory


async def run_case(settings: Settings, case: EvalCase) -> CaseRun:
    from google.adk.agents import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    auth = load_demo_context(case.auth_context_id)
    retriever_factory, repo_factory = _factories(settings, case)
    agent = build_agent(settings, StaticIdentityProvider(auth), retriever_factory, repo_factory)
    runner = Runner(
        app_name=APP_NAME,
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    run = CaseRun()
    started = time.monotonic()
    final_parts: list[str] = []
    try:
        async with asyncio.timeout(settings.agent_run_timeout_seconds):
            async for event in runner.run_async(
                user_id=auth.subject,
                session_id=f"eval-{case.case_id}-{uuid.uuid4().hex[:8]}",
                new_message=types.Content(role="user", parts=[types.Part(text=case.user_message)]),
                run_config=RunConfig(max_llm_calls=settings.max_llm_calls),
            ):
                for fc in event.get_function_calls() or []:
                    run.tool_calls.append({"name": fc.name, "args": dict(fc.args or {})})
                for fr in event.get_function_responses() or []:
                    run.tool_responses.append({"name": fr.name, "response": _plain(fr.response)})
                if event.is_final_response() and event.content and event.content.parts:
                    final_parts = [p.text for p in event.content.parts if p.text]
    except TimeoutError:
        run.error = f"agent run exceeded {settings.agent_run_timeout_seconds}s"
    except Exception as e:  # LlmCallsLimitExceededError, API errors, ...
        run.error = f"{type(e).__name__}: {e}"
    finally:
        await runner.close()
    run.final_text = "\n".join(final_parts).strip()
    run.seconds = round(time.monotonic() - started, 1)
    return run


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    return obj
