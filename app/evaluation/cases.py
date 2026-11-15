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
METADATA_PATH = PROJECT_ROOT / "data" / "reference" / "dataset_metadata.json"

# Setups that only need fault injection around read tools (any mode).
READ_ONLY_SETUP_KEYS = frozenset({"tool_fault", "retrieval_fault"})
# Setups that seed proposal/approval state (emulator mode only).
WRITE_SETUP_KEYS = frozenset(
    {
        "proposal",
        "approval_fixture",
        "execution_requests",
        "prior_approved_proposal",
        "proposal_id",
        "expires_at",
    }
)
# Dataset tool names -> this agent's tool names.
TOOL_ALIASES = {"get_open_work_orders": "list_my_work_orders"}
# Cases whose behaviour needs the creation tools even without write setup.
NEEDS_CREATION_TOOLS = {"EVAL-006"}

MODES = ("cloud-readonly", "emulator")


def snapshot_at() -> str:
    return json.loads(METADATA_PATH.read_text())["snapshot_at"]


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
    def needs_writes(self) -> bool:
        return self.case_id in NEEDS_CREATION_TOOLS or bool(set(self.setup) & WRITE_SETUP_KEYS)

    @property
    def expected_new_work_orders(self) -> int:
        """Exactly one only when an approved proposal is executed (EVAL-014 style)."""
        return 1 if "execution_requests" in self.setup else 0

    def skip_reason(self, mode: str) -> str | None:
        unknown = set(self.setup) - READ_ONLY_SETUP_KEYS - WRITE_SETUP_KEYS
        if unknown:
            return f"unsupported setup keys {sorted(unknown)}"
        if mode == "cloud-readonly" and self.needs_writes:
            return "needs work-order creation; run with --mode emulator"
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
