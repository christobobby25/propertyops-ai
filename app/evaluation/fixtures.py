"""Translate dataset setup fixtures into emulator state and a confirmation responder.

Fixture gaps (filled here, and reported in every evaluation report):
* EVAL-014 ``proposal`` has no policy_section_ids/reported_facts → empty.
  Its client ``idempotency_key`` is recorded but not trusted: execution keys
  are derived from (proposal_id, payload_hash).
* EVAL-015 ``prior_approved_proposal`` and EVAL-020 (``proposal_id``,
  ``expires_at``) carry no proposal content → a plausible HVAC proposal for
  the resident's own unit is synthesized.
* "Prior approval" is modelled as APPROVAL_REQUESTED bound to a fixture
  function_call_id, which no new tool call can reuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.agent.proposals import Proposal, ProposalContent, Status
from app.auth import AuthContext
from app.evaluation.cases import EvalCase

# Mirrors the dataset's own RES-002 HVAC proposal (EVAL-014), including the
# reported temperature, so the synthesized fixture is internally consistent.
SYNTHETIC_CONTENT = ProposalContent(
    category="hvac",
    summary="AC not cooling",
    description="Indoor temperature is 80 F; cooling has stopped again.",
    priority="routine",
    entry_permission="appointment_required",
    reported_facts="indoor temperature 80 F",
)


class FixtureError(ValueError):
    pass


@dataclass
class CaseFixture:
    proposals: list[Proposal] = field(default_factory=list)
    approve_proposal_id: str | None = None
    approve_payload_hash: str | None = None
    approve: bool = False
    turns: int = 1
    gaps: list[str] = field(default_factory=list)

    def respond_to_confirmation(self, payload: dict | None, auth: AuthContext) -> bool | None:
        """Decision the *client* sends for a confirmation request; None = no response."""
        if self.approve_proposal_id is None or not payload:
            return None
        if payload.get("proposal_id") != self.approve_proposal_id:
            return None  # approval is bound to one specific proposal
        if self.approve_payload_hash and payload.get("payload_hash") != self.approve_payload_hash:
            return None  # contents changed since approval was given
        return self.approve


def _scope(auth: AuthContext) -> tuple[str, str, str]:
    return (
        auth.resident_id,
        next(iter(auth.allowed_property_ids)),
        next(iter(auth.allowed_unit_ids)),
    )


def build_fixture(case: EvalCase, auth: AuthContext, now: datetime) -> CaseFixture:
    s = case.setup
    fx = CaseFixture()
    resident_id, property_id, unit_id = _scope(auth)

    def make(pid, content, created, expires, status=Status.AWAITING_APPROVAL, fc=None):
        return Proposal(
            proposal_id=pid,
            owner_subject=auth.subject,
            resident_id=resident_id,
            property_id=property_id,
            unit_id=unit_id,
            content=content,
            created_at=created,
            expires_at=expires,
            status=status,
            approval_function_call_id=fc,
            approval_requested_at=created if fc else None,
            updated_at=created,
        ).sealed()

    if "proposal" in s:  # EVAL-014
        f = s["proposal"]
        if (f["resident_id"], f["property_id"], f["unit_id"]) != (
            resident_id,
            property_id,
            unit_id,
        ):
            raise FixtureError(f"{case.case_id}: proposal scope does not match identity")
        content = ProposalContent(
            category=f["category"],
            summary=f["summary"],
            description=f["description"],
            priority=f["priority"],
            entry_permission=f["entry_permission"],
        )
        p = make(
            f["proposal_id"],
            content,
            datetime.fromisoformat(f["created_at"]),
            datetime.fromisoformat(f["expires_at"]),
        )
        fx.proposals.append(p)
        fx.gaps.append("proposal fixture lacks policy_section_ids/reported_facts (left empty)")
        if "execution_requests" in s:
            fx.turns = int(s["execution_requests"].get("count", 1))
            fx.gaps.append(
                "client idempotency_key recorded but not trusted; execution key is derived "
                "from proposal_id + payload_hash"
            )

    if "prior_approved_proposal" in s:  # EVAL-015
        p = make(
            f"PROPOSAL-{case.case_id}",
            SYNTHETIC_CONTENT,
            now - timedelta(minutes=5),
            now + timedelta(minutes=10),
            status=Status.APPROVAL_REQUESTED,
            fc="fixture-prior-approval",
        )
        fx.proposals.append(p)
        fx.gaps.append("prior_approved_proposal has no content; synthesized HVAC proposal")

    if "proposal_id" in s and "proposal" not in s:  # EVAL-020
        expires = datetime.fromisoformat(s["expires_at"])
        p = make(s["proposal_id"], SYNTHETIC_CONTENT, expires - timedelta(minutes=15), expires)
        fx.proposals.append(p)
        fx.gaps.append("expired proposal fixture has no content; synthesized HVAC proposal")

    if approval := s.get("approval_fixture"):
        if approval.get("actor_subject") != auth.subject:
            raise FixtureError(f"{case.case_id}: approval actor is not the authenticated user")
        target = fx.proposals[0] if fx.proposals else None
        if target is None:
            raise FixtureError(f"{case.case_id}: approval fixture without a proposal")
        fx.approve_proposal_id = target.proposal_id
        fx.approve = bool(approval.get("approved"))
        if approval.get("bind_to_proposal_payload"):
            fx.approve_payload_hash = target.payload_hash
    return fx
