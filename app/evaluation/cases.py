"""Evaluation cases. Read ONLY by the live evaluator — never by the agent.

Expected answers stay here and in the judge prompt; the agent receives only
``user_message`` and a backend-bound identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

EVAL_CASES_PATH = PROJECT_ROOT / "data" / "eval" / "evaluation_cases.json"

# Setups the Stage 1 harness can simulate (fault injection around read tools).
SUPPORTED_SETUP_KEYS = frozenset({"tool_fault", "retrieval_fault"})
# Dataset tool names -> this agent's tool names.
TOOL_ALIASES = {"get_open_work_orders": "list_my_work_orders"}
# Cases whose required behaviour is the Stage 2 propose/approve/create flow.
STAGE2_CASES = {
    "EVAL-006": "requires presenting a request proposal for approval (Stage 2)",
}


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    auth_context_id: str
    user_message: str
    setup: dict
    relevant_document_ids: tuple[str, ...]
    relevant_work_order_ids: tuple[str, ...]
    required_behaviors: tuple[str, ...]
    forbidden_behaviors: tuple[str, ...]

    @property
    def skip_reason(self) -> str | None:
        if self.case_id in STAGE2_CASES:
            return STAGE2_CASES[self.case_id]
        unsupported = set(self.setup) - SUPPORTED_SETUP_KEYS
        if unsupported:
            return f"setup {sorted(unsupported)} requires work-order creation (Stage 2)"
        return None


def load_cases(path: Path = EVAL_CASES_PATH) -> list[EvalCase]:
    out = []
    for c in json.loads(path.read_text()):
        e = c["expected"]
        out.append(
            EvalCase(
                case_id=c["case_id"],
                auth_context_id=c["auth_context_id"],
                user_message=c["user_message"],
                setup=c.get("setup") or {},
                relevant_document_ids=tuple(e["relevant_document_ids"]),
                relevant_work_order_ids=tuple(e["relevant_work_order_ids"]),
                required_behaviors=tuple(e["required_behaviors"]),
                forbidden_behaviors=tuple(e["forbidden_behaviors"]),
            )
        )
    return out
