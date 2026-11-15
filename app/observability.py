"""Request correlation and agent telemetry (structured logs).

* ``request_context`` carries a request ID and the Cloud Trace ID (from
  ``X-Cloud-Trace-Context`` on Cloud Run) into every log line.
* ADK callbacks log one event per model call (latency, token usage) and per
  tool call (latency, outcome, error kind), and flag authorization denials.

Events (``event`` field): http_request, model_call, tool_call, authz_denied,
turn_complete, approval_decision, auth_failed. Cloud Logging log-based
metrics can be defined on these fields (see README).
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

log = logging.getLogger("propertyops.telemetry")

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
trace_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace", default=None)

AUTHZ_ERRORS = frozenset({"unauthenticated", "forbidden", "not_found", "invalid_arguments"})
_MODEL_START = "temp:telemetry_model_start"


def trace_from_header(header: str | None, project: str | None) -> str | None:
    """'TRACE_ID/SPAN_ID;o=1' -> 'projects/<p>/traces/<TRACE_ID>'."""
    if not header or not project:
        return None
    trace_id = header.split("/", 1)[0].strip()
    return f"projects/{project}/traces/{trace_id}" if trace_id else None


def new_request_context(trace_header: str | None, project: str | None):
    return (
        request_id_var.set(uuid.uuid4().hex[:16]),
        trace_var.set(trace_from_header(trace_header, project)),
    )


# --- ADK callbacks -------------------------------------------------------------------


def before_model(callback_context: Any, llm_request: Any) -> None:
    callback_context.state[_MODEL_START] = time.monotonic()
    return None


def after_model(callback_context: Any, llm_response: Any) -> None:
    started = callback_context.state.get(_MODEL_START)
    usage = getattr(llm_response, "usage_metadata", None)
    log.info(
        "model_call",
        extra={
            "event": "model_call",
            "latency_ms": round((time.monotonic() - started) * 1000) if started else None,
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
            "error_code": getattr(llm_response, "error_code", None),
            "subject": getattr(callback_context, "user_id", None),
        },
    )
    return None


def _tool_key(tool_context: Any) -> str:
    return f"temp:telemetry_tool_{getattr(tool_context, 'function_call_id', '')}"


def before_tool(tool: Any, args: dict, tool_context: Any) -> None:
    tool_context.state[_tool_key(tool_context)] = time.monotonic()
    return None


def after_tool(tool: Any, args: dict, tool_context: Any, tool_response: Any) -> None:
    started = tool_context.state.get(_tool_key(tool_context))
    resp = tool_response if isinstance(tool_response, dict) else {}
    error = resp.get("error")
    subject = getattr(tool_context, "user_id", None)
    fields = {
        "event": "tool_call",
        "tool": getattr(tool, "name", None),
        "latency_ms": round((time.monotonic() - started) * 1000) if started else None,
        "status": resp.get("status"),
        "error_kind": error,
        "subject": subject,
    }
    log.log(logging.WARNING if error else logging.INFO, "tool_call", extra=fields)
    if error in AUTHZ_ERRORS:
        log.warning(
            "authz_denied",
            extra={
                "event": "authz_denied",
                "tool": fields["tool"],
                "reason": error,
                "subject": subject,
            },
        )
    return None
