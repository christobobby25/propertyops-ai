"""Per-turn budgets and tool-argument allowlisting, as ADK callbacks.

Counters live in ``temp:`` session state, which ADK scopes to a single
invocation (one user turn), so limits reset every turn.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.agent.tools import TOOL_ARGUMENTS, error

log = logging.getLogger(__name__)

LLM_CALLS_KEY = "temp:propertyops_llm_calls"
TOOL_CALLS_KEY = "temp:propertyops_tool_calls"

LIMIT_MESSAGE = (
    "I couldn't finish looking into this within my processing limits for one "
    "message, so I can't give a verified answer. Please try again or ask a "
    "narrower question."
)


def make_before_tool_callback(max_tool_calls: int):
    def before_tool(tool: Any, args: dict[str, Any], tool_context: Any) -> dict | None:
        name = getattr(tool, "name", "")
        allowed = TOOL_ARGUMENTS.get(name)
        if allowed is None:
            log.warning("blocked unknown tool %r", name)
            return error("unknown_tool", f"Tool {name!r} is not available.")

        unexpected = sorted(set(args) - allowed)
        if unexpected:
            log.warning("blocked %s with unexpected args %s", name, unexpected)
            return error(
                "invalid_arguments",
                f"{name} does not accept {', '.join(unexpected)}. Access scope "
                "comes from the user's verified identity and cannot be set by arguments.",
            )

        count = int(tool_context.state.get(TOOL_CALLS_KEY, 0)) + 1
        tool_context.state[TOOL_CALLS_KEY] = count
        if count > max_tool_calls:
            log.warning("tool call limit (%d) reached", max_tool_calls)
            return error(
                "tool_call_limit",
                "Tool call limit for this message reached. Answer with what has "
                "been verified so far and say what could not be checked.",
            )
        return None

    return before_tool


def make_before_model_callback(max_llm_calls: int):
    def before_model(callback_context: Any, llm_request: Any) -> LlmResponse | None:
        count = int(callback_context.state.get(LLM_CALLS_KEY, 0)) + 1
        callback_context.state[LLM_CALLS_KEY] = count
        if count > max_llm_calls:
            log.warning("model call limit (%d) reached", max_llm_calls)
            return LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=LIMIT_MESSAGE)])
            )
        return None

    return before_model
