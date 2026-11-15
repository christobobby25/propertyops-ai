"""Run evaluation cases against the real agent (live Gemini).

Modes
-----
* ``cloud-readonly``: cloud Firestore, read-only. Creation tools are NOT
  registered, so the agent cannot write. Cases needing writes are skipped.
* ``emulator``: every Firestore read/write goes to the local emulator
  (demo-* project, anonymous credentials). Each case starts from a fresh
  seed. Creation tools are enabled; the harness plays the user's client and
  answers ADK confirmation requests only as the case's approval fixture says.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.clock import FixedClock
from app.agent.factory import build_agent, firestore_factories
from app.agent.identity import StaticIdentityProvider
from app.config import Settings
from app.demo_auth import load_demo_context
from app.evaluation.cases import TOOL_ALIASES, EvalCase, snapshot_at
from app.evaluation.fixtures import CaseFixture, build_fixture

APP_NAME = "propertyops_eval"
CONFIRMATION_CALL = "adk_request_confirmation"


@dataclass
class CaseRun:
    final_text: str = ""
    turn_texts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)  # {"name", "args"}
    tool_responses: list[dict] = field(default_factory=list)  # {"name", "response"}
    confirmations: list[dict] = field(default_factory=list)
    new_work_orders: list[dict] = field(default_factory=list)
    fixture_gaps: list[str] = field(default_factory=list)
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

    def published_sections(self, *a, **kw):
        return self.inner.published_sections(*a, **kw)


# --- Environments ---------------------------------------------------------------


class EvalEnvironment:
    def __init__(self, mode: str, settings: Settings, chunk_docs: dict | None = None):
        self.mode = mode
        self.settings = settings
        self.chunk_docs = chunk_docs
        self.clock = FixedClock(datetime.fromisoformat(snapshot_at()))
        self._db = None

    @classmethod
    def create(cls, mode: str, settings: Settings) -> EvalEnvironment:
        """Validate the Firestore target up front; fail closed, never fall back."""
        from app.gcp import firestore_client

        if mode == "emulator":
            s = settings.model_copy(update={"firestore_target": "emulator"})
            env = cls(mode, s)
            env._db = firestore_client(s)  # raises if host missing/unreachable
            from app import emulator
            from app.embeddings import VertexEmbedder

            env.chunk_docs = emulator.build_policy_chunk_docs(s, VertexEmbedder(s))
            return env
        s = settings.model_copy(update={"firestore_target": "cloud"})
        env = cls(mode, s)
        env._db = firestore_client(s)  # raises if FIRESTORE_EMULATOR_HOST is set
        return env

    @property
    def writes_enabled(self) -> bool:
        return self.mode == "emulator"

    def reset(self) -> None:
        if self.writes_enabled:
            from app import emulator

            emulator.reset(self._db, self.chunk_docs)

    def seed_fixture(self, fx: CaseFixture) -> None:
        if not fx.proposals:
            return
        if not self.writes_enabled:
            raise RuntimeError("proposal fixtures require emulator mode")
        from app.agent.proposals import PROPOSALS

        for p in fx.proposals:
            self._db.collection(PROPOSALS).document(p.proposal_id).set(p.to_doc())

    def new_work_orders(self) -> list[dict]:
        if not self.writes_enabled:
            return []
        from app.seed_data import load_seed_collection

        seed_ids = {w["work_order_id"] for w in load_seed_collection("work_orders")}
        return [
            s.to_dict() for s in self._db.collection("work_orders").stream() if s.id not in seed_ids
        ]

    def factories(self, case: EvalCase):
        f = firestore_factories(self.settings, clock=self.clock)
        retriever_factory, repo_factory = f.retriever, f.work_orders

        fault = case.setup.get("tool_fault")
        if fault and TOOL_ALIASES.get(fault["tool"], fault["tool"]) == "list_my_work_orders":
            if fault.get("behavior") == "timeout":
                delay = self.settings.tool_timeout_seconds + 2
                repo_factory = lambda: _SlowRepo(delay)  # noqa: E731

        if rfault := case.setup.get("retrieval_fault"):
            base = retriever_factory

            def retriever_factory():
                r = base()
                r._store = _InjectingStore(
                    r._store, rfault["document_id"], rfault["append_untrusted_text"]
                )
                return r

        proposals = f.proposals if self.writes_enabled else None
        return retriever_factory, repo_factory, proposals


# --- Running a case ---------------------------------------------------------------


async def run_case(env: EvalEnvironment, case: EvalCase) -> CaseRun:
    from google.adk.agents import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    s = env.settings
    auth = load_demo_context(case.auth_context_id)
    run = CaseRun()
    started = time.monotonic()

    env.reset()
    fx = build_fixture(case, auth, env.clock.now())
    env.seed_fixture(fx)
    run.fixture_gaps = list(fx.gaps)

    retriever_factory, repo_factory, proposals = env.factories(case)
    agent = build_agent(s, StaticIdentityProvider(auth), retriever_factory, repo_factory, proposals)
    runner = Runner(
        app_name=APP_NAME,
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    session_id = f"eval-{case.case_id}-{uuid.uuid4().hex[:8]}"

    async def one_invocation(content) -> Any:
        pending = None
        final: list[str] = []
        async for event in runner.run_async(
            user_id=auth.subject,
            session_id=session_id,
            new_message=content,
            run_config=RunConfig(max_llm_calls=s.max_llm_calls),
        ):
            for fc in event.get_function_calls() or []:
                if fc.name == CONFIRMATION_CALL:
                    pending = fc
                    continue
                run.tool_calls.append({"name": fc.name, "args": dict(fc.args or {})})
            for fr in event.get_function_responses() or []:
                if fr.name != CONFIRMATION_CALL:
                    run.tool_responses.append({"name": fr.name, "response": _plain(fr.response)})
            if event.is_final_response() and event.content and event.content.parts:
                if text := "\n".join(p.text for p in event.content.parts if p.text).strip():
                    final.append(text)
        if final:
            run.turn_texts.append(final[-1])
        return pending

    try:
        async with asyncio.timeout(s.agent_run_timeout_seconds):
            for _ in range(fx.turns):
                content = types.Content(role="user", parts=[types.Part(text=case.user_message)])
                while content is not None:
                    pending = await one_invocation(content)
                    content = None
                    if pending is None:
                        break
                    payload = (pending.args or {}).get("toolConfirmation", {}).get("payload")
                    decision = fx.respond_to_confirmation(payload, auth)
                    run.confirmations.append(
                        {
                            "proposal_id": (payload or {}).get("proposal_id"),
                            "client_response": decision,
                        }
                    )
                    if decision is not None:
                        content = types.Content(
                            role="user",
                            parts=[
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        id=pending.id,
                                        name=CONFIRMATION_CALL,
                                        response={"confirmed": decision},
                                    )
                                )
                            ],
                        )
    except TimeoutError:
        run.error = f"agent run exceeded {s.agent_run_timeout_seconds}s"
    except Exception as e:  # LlmCallsLimitExceededError, API errors, ...
        run.error = f"{type(e).__name__}: {e}"
    finally:
        await runner.close()

    run.final_text = "\n\n---\n\n".join(run.turn_texts).strip()
    run.new_work_orders = env.new_work_orders()
    run.seconds = round(time.monotonic() - started, 1)
    return run


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    return obj
