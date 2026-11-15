"""Chat service: runs the ADK agent for an authenticated subject.

Approval boundary for the web UI: when the agent requests confirmation, the
service records the pending confirmation for (subject, session_id) and returns
the backend-rendered proposal to the browser. ``respond`` only accepts that
exact pending confirmation from the same subject; it then sends ADK the
confirmation function response, and the tool re-validates everything inside
its transaction. The model never sees or produces the approval itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

log = logging.getLogger("propertyops.chat")

APP_NAME = "propertyops_web"
CONFIRMATION_CALL = "adk_request_confirmation"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_MESSAGE_CHARS = 2000


class ChatError(ValueError):
    pass


@dataclass
class TurnResult:
    reply: str = ""
    citations: dict[str, dict] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    pending_confirmation: dict | None = None
    created_work_order: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "reply": self.reply,
            "citations": self.citations,
            "tools": self.tools,
            "pending_confirmation": self.pending_confirmation,
            "created_work_order": self.created_work_order,
            "error": self.error,
        }


def check_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise ChatError("invalid session_id")
    return session_id


class ChatService:
    def __init__(self, settings: Settings, agent, runner=None):
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        self.s = settings
        self.runner = runner or Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )
        self._pending: dict[tuple[str, str], dict] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def send(self, subject: str, session_id: str, message: str) -> TurnResult:
        from google.genai import types

        check_session_id(session_id)
        message = (message or "").strip()
        if not message or len(message) > MAX_MESSAGE_CHARS:
            raise ChatError(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
        key = (subject, session_id)
        async with self._lock(key):
            # A new user message abandons any unanswered dialog for this session.
            self._pending.pop(key, None)
            content = types.Content(role="user", parts=[types.Part(text=message)])
            return await self._run(subject, session_id, content)

    async def respond(
        self, subject: str, session_id: str, confirmation_id: str, approve: bool
    ) -> TurnResult:
        from google.genai import types

        check_session_id(session_id)
        key = (subject, session_id)
        async with self._lock(key):
            pending = self._pending.get(key)
            if not pending or pending["confirmation_id"] != confirmation_id:
                raise ChatError("no such pending confirmation for this session")
            self._pending.pop(key)
            log.info(
                "approval_decision",
                extra={
                    "event": "approval_decision",
                    "subject": subject,
                    "proposal_id": pending.get("proposal_id"),
                    "approved": bool(approve),
                },
            )
            content = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=confirmation_id,
                            name=CONFIRMATION_CALL,
                            response={"confirmed": bool(approve)},
                        )
                    )
                ],
            )
            return await self._run(subject, session_id, content)

    async def _run(self, subject: str, session_id: str, content) -> TurnResult:
        from google.adk.agents import RunConfig

        result = TurnResult()
        finals: list[str] = []
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.s.agent_run_timeout_seconds):
                async for event in self.runner.run_async(
                    user_id=subject,
                    session_id=session_id,
                    new_message=content,
                    run_config=RunConfig(max_llm_calls=self.s.max_llm_calls),
                ):
                    self._absorb(event, subject, session_id, result)
                    if event.is_final_response() and event.content and event.content.parts:
                        text = "\n".join(p.text for p in event.content.parts if p.text).strip()
                        if text:
                            finals.append(text)
        except TimeoutError:
            result.error = "timeout"
        except Exception:
            log.exception("agent run failed", extra={"event": "agent_error", "subject": subject})
            result.error = "agent_error"
        result.reply = (
            finals[-1]
            if finals
            else (
                "Sorry, I couldn't complete that right now. Please try again."
                if result.error
                else ""
            )
        )
        log.info(
            "turn_complete",
            extra={
                "event": "turn_complete",
                "subject": subject,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "tools": result.tools,
                "error": result.error,
                "pending_confirmation": bool(result.pending_confirmation),
            },
        )
        return result

    def _absorb(self, event, subject: str, session_id: str, result: TurnResult) -> None:
        for fc in event.get_function_calls() or []:
            if fc.name == CONFIRMATION_CALL:
                conf = (fc.args or {}).get("toolConfirmation") or {}
                payload = conf.get("payload") or {}
                pending = {
                    "confirmation_id": fc.id,
                    "proposal_id": payload.get("proposal_id"),
                    "display": conf.get("hint", ""),
                }
                self._pending[(subject, session_id)] = pending
                result.pending_confirmation = pending
            else:
                result.tools.append(fc.name)
        for fr in event.get_function_responses() or []:
            resp: Any = fr.response if isinstance(fr.response, dict) else {}
            if fr.name == "search_policies":
                for p in resp.get("policies", []):
                    result.citations[p["section_id"]] = {
                        "title": p.get("document_title"),
                        "heading": p.get("heading"),
                        "text": p.get("text"),
                        "version": p.get("version"),
                    }
            elif fr.name == "submit_work_order_proposal" and resp.get("status") == "executed":
                result.created_work_order = {
                    "work_order_id": resp.get("work_order_id"),
                    "newly_created": resp.get("newly_created"),
                }
