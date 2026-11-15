"""Approval-controlled work-order creation.

State machine (see README):

    prepare ─▶ AWAITING_APPROVAL ─submit─▶ APPROVAL_REQUESTED ─confirmed─▶ EXECUTED
                 │ new prepare ─▶ SUPERSEDED      │ confirmed=false ─▶ DECLINED
                 │ cancel ──────▶ CANCELLED       │ cancel ──────────▶ CANCELLED
                 └ past expiry ─▶ EXPIRED         └ past expiry ─────▶ EXPIRED

* Proposals are immutable; any content change is a new proposal and
  supersedes the owner's previous open proposal (one open proposal per owner).
* Approval is bound to (proposal_id, payload_hash, approval function_call_id).
* Execution is one transaction that re-validates everything, writes the work
  order, increments the ID counter, records the execution, and marks the
  proposal EXECUTED. The execution key makes retries return the same ID.
* Rules live in pure functions shared by the Firestore and in-memory
  repositories; repositories only provide storage and atomicity.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from app.agent.clock import Clock
from app.agent.work_orders import ACTIVE_STATUSES, WorkOrderRepository, project
from app.auth import AuthContext

CATEGORIES = ("hvac", "plumbing", "electrical", "appliance", "access")
PRIORITIES = ("routine", "urgent", "emergency")
ENTRY_PERMISSIONS = ("permission_granted", "appointment_required")
MAX_SUMMARY = 120
MAX_DESCRIPTION = 2000
MAX_FACTS = 1000

PROPOSALS = "proposals"
EXECUTIONS = "work_order_executions"
COUNTERS = "counters"
WORK_ORDER_COUNTER = "work_orders"


class Status(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVAL_REQUESTED = "approval_requested"
    EXECUTED = "executed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


OPEN = frozenset({Status.AWAITING_APPROVAL, Status.APPROVAL_REQUESTED})


class ProposalError(Exception):
    """A rule violation. ``kind`` is returned to the model; details are safe to show."""

    def __init__(self, kind: str, message: str, **extra: Any):
        super().__init__(message)
        self.kind, self.message, self.extra = kind, message, extra


def _not_found() -> ProposalError:
    # Foreign and missing proposals are indistinguishable to the caller.
    return ProposalError("not_found", "No such proposal for this account.")


# --- Content and integrity ---------------------------------------------------


def canonical_ts(dt: datetime) -> str:
    """Timezone-independent form, stable across Firestore round trips (UTC, µs)."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class ProposalContent:
    category: str
    summary: str
    description: str
    priority: str
    entry_permission: str
    policy_section_ids: tuple[str, ...] = ()
    reported_facts: str = ""

    def validate(self) -> None:
        def require(cond: bool, field_: str, msg: str):
            if not cond:
                raise ProposalError("invalid_field", f"{field_}: {msg}", field=field_)

        require(self.category in CATEGORIES, "category", f"must be one of {list(CATEGORIES)}")
        require(self.priority in PRIORITIES, "priority", f"must be one of {list(PRIORITIES)}")
        require(
            self.entry_permission in ENTRY_PERMISSIONS,
            "entry_permission",
            f"must be one of {list(ENTRY_PERMISSIONS)}",
        )
        require(0 < len(self.summary.strip()) <= MAX_SUMMARY, "summary", "1-120 characters")
        require(
            0 < len(self.description.strip()) <= MAX_DESCRIPTION,
            "description",
            "1-2000 characters",
        )
        require(len(self.reported_facts) <= MAX_FACTS, "reported_facts", "too long")


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    owner_subject: str
    resident_id: str
    property_id: str
    unit_id: str
    content: ProposalContent
    created_at: datetime
    expires_at: datetime
    status: Status = Status.AWAITING_APPROVAL
    payload_hash: str = ""
    acknowledged_work_order_ids: tuple[str, ...] = ()
    approval_function_call_id: str | None = None
    approval_requested_at: datetime | None = None
    work_order_id: str | None = None
    execution_key: str | None = None
    superseded_by: str | None = None
    updated_at: datetime | None = None

    def canonical_payload(self) -> dict:
        """Exactly what the resident approves. Changing any of it changes the hash."""
        c = self.content
        return {
            "proposal_id": self.proposal_id,
            "resident_id": self.resident_id,
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "category": c.category,
            "summary": c.summary,
            "description": c.description,
            "priority": c.priority,
            "entry_permission": c.entry_permission,
            "policy_section_ids": list(c.policy_section_ids),
            "reported_facts": c.reported_facts,
            "acknowledged_work_order_ids": sorted(self.acknowledged_work_order_ids),
            "created_at": canonical_ts(self.created_at),
            "expires_at": canonical_ts(self.expires_at),
        }

    def compute_hash(self) -> str:
        blob = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def sealed(self) -> Proposal:
        return replace(self, payload_hash=self.compute_hash())

    @property
    def execution_key_value(self) -> str:
        return hashlib.sha256(f"{self.proposal_id}:{self.payload_hash}".encode()).hexdigest()

    # Firestore document mapping
    def to_doc(self) -> dict:
        c = self.content
        return {
            "proposal_id": self.proposal_id,
            "owner_subject": self.owner_subject,
            "resident_id": self.resident_id,
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "category": c.category,
            "summary": c.summary,
            "description": c.description,
            "priority": c.priority,
            "entry_permission": c.entry_permission,
            "policy_section_ids": list(c.policy_section_ids),
            "reported_facts": c.reported_facts,
            "acknowledged_work_order_ids": list(self.acknowledged_work_order_ids),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": str(self.status),
            "payload_hash": self.payload_hash,
            "approval_function_call_id": self.approval_function_call_id,
            "approval_requested_at": self.approval_requested_at,
            "work_order_id": self.work_order_id,
            "execution_key": self.execution_key,
            "superseded_by": self.superseded_by,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_doc(cls, d: dict) -> Proposal:
        return cls(
            proposal_id=d["proposal_id"],
            owner_subject=d["owner_subject"],
            resident_id=d["resident_id"],
            property_id=d["property_id"],
            unit_id=d["unit_id"],
            content=ProposalContent(
                category=d["category"],
                summary=d["summary"],
                description=d["description"],
                priority=d["priority"],
                entry_permission=d["entry_permission"],
                policy_section_ids=tuple(d.get("policy_section_ids") or ()),
                reported_facts=d.get("reported_facts") or "",
            ),
            created_at=d["created_at"],
            expires_at=d["expires_at"],
            status=Status(d["status"]),
            payload_hash=d["payload_hash"],
            acknowledged_work_order_ids=tuple(d.get("acknowledged_work_order_ids") or ()),
            approval_function_call_id=d.get("approval_function_call_id"),
            approval_requested_at=d.get("approval_requested_at"),
            work_order_id=d.get("work_order_id"),
            execution_key=d.get("execution_key"),
            superseded_by=d.get("superseded_by"),
            updated_at=d.get("updated_at"),
        )


@dataclass(frozen=True)
class ExecutionResult:
    work_order_id: str
    proposal_id: str
    created: bool  # False when an earlier execution is being returned (idempotent replay)
    work_order: dict = field(default_factory=dict)


# --- Pure rules (shared by all repositories) -----------------------------------


def resident_scope(auth: AuthContext) -> tuple[str, str, str]:
    """(resident_id, property_id, unit_id) from trusted identity; fail closed."""
    if auth.role != "resident" or not auth.resident_id:
        raise ProposalError("forbidden", "Only residents can request maintenance here.")
    if len(auth.allowed_property_ids) != 1 or len(auth.allowed_unit_ids) != 1:
        raise ProposalError("scope_ambiguous", "This account is not linked to exactly one unit.")
    return (
        auth.resident_id,
        next(iter(auth.allowed_property_ids)),
        next(iter(auth.allowed_unit_ids)),
    )


def check_owner(p: Proposal | None, auth: AuthContext) -> Proposal:
    if p is None:
        raise _not_found()
    try:
        resident_id, property_id, unit_id = resident_scope(auth)
    except ProposalError:
        raise _not_found() from None
    if (
        p.owner_subject != auth.subject
        or p.resident_id != resident_id
        or p.property_id != property_id
        or p.unit_id != unit_id
    ):
        raise _not_found()
    return p


def effective_status(p: Proposal, now: datetime) -> Status:
    if p.status in OPEN and now >= p.expires_at:
        return Status.EXPIRED
    return p.status


_STATUS_ERRORS = {
    Status.EXPIRED: ("expired", "This proposal has expired. Prepare a new one for approval."),
    Status.CANCELLED: ("cancelled", "This proposal was cancelled."),
    Status.SUPERSEDED: ("superseded", "This proposal was replaced by a newer one."),
    Status.DECLINED: ("declined", "The resident declined this proposal."),
}


def require_open(p: Proposal, now: datetime) -> None:
    status = effective_status(p, now)
    if status in _STATUS_ERRORS:
        kind, msg = _STATUS_ERRORS[status]
        raise ProposalError(kind, msg, proposal_status=str(status))
    if p.compute_hash() != p.payload_hash:
        raise ProposalError("integrity", "Proposal contents changed after it was created.")


def check_executable(p: Proposal, function_call_id: str | None, now: datetime) -> None:
    require_open(p, now)
    if p.status != Status.APPROVAL_REQUESTED:
        raise ProposalError("approval_missing", "This proposal has not been sent for approval.")
    if not function_call_id or p.approval_function_call_id != function_call_id:
        raise ProposalError(
            "approval_mismatch",
            "This approval does not match the latest approval request for the proposal.",
        )


def active_matches(unit_orders: Iterable[dict], category: str) -> list[dict]:
    return [
        wo
        for wo in unit_orders
        if wo.get("category") == category and wo.get("status") in ACTIVE_STATUSES
    ]


def duplicate_blockers(p: Proposal, unit_orders: Iterable[dict]) -> list[dict]:
    acknowledged = set(p.acknowledged_work_order_ids)
    return [
        wo
        for wo in active_matches(unit_orders, p.content.category)
        if wo["work_order_id"] not in acknowledged and wo.get("proposal_id") != p.proposal_id
    ]


def possible_duplicate(matches: list[dict]) -> ProposalError:
    return ProposalError(
        "possible_duplicate",
        "There are active requests in the same category for this unit. Show them to the "
        "resident and ask whether this is a different issue or the severity has changed. "
        "Only if it is a different issue, prepare again listing their IDs in "
        "acknowledged_work_order_ids.",
        matching_work_orders=[project(wo, "resident") for wo in matches],
    )


def build_work_order(p: Proposal, work_order_id: str, now: datetime) -> dict:
    c = p.content
    ts = now.isoformat()
    return {
        "work_order_id": work_order_id,
        "property_id": p.property_id,
        "unit_id": p.unit_id,
        "resident_id": p.resident_id,
        "category": c.category,
        "summary": c.summary,
        "description": c.description,
        "priority": c.priority,
        "status": "open",
        "created_at": ts,
        "updated_at": ts,
        "completed_at": None,
        "scheduled_window": None,
        "entry_permission": c.entry_permission,
        "resident_visible_update": "Request received. Awaiting initial contact.",
        "internal_notes": "",
        "proposal_id": p.proposal_id,
        "created_by_subject": p.owner_subject,
    }


def format_work_order_id(n: int) -> str:
    return f"WO-{n:03d}"


# --- Repository interface ------------------------------------------------------


class ProposalRepository(Protocol):
    def create_superseding(self, p: Proposal, now: datetime) -> None: ...
    def get(self, proposal_id: str) -> Proposal | None: ...
    def list_for_owner(self, owner_subject: str) -> list[Proposal]: ...
    def request_approval(
        self, proposal_id: str, auth: AuthContext, function_call_id: str, now: datetime
    ) -> Proposal: ...
    def close(
        self,
        proposal_id: str,
        auth: AuthContext,
        to_status: Status,
        now: datetime,
        function_call_id: str | None = None,
    ) -> Proposal: ...
    def execute(
        self, proposal_id: str, auth: AuthContext, function_call_id: str, now: datetime
    ) -> ExecutionResult: ...


def _close_rules(
    p: Proposal, to_status: Status, now: datetime, function_call_id: str | None
) -> Proposal:
    if to_status == Status.EXPIRED:
        if effective_status(p, now) != Status.EXPIRED or p.status not in OPEN:
            return p
        return replace(p, status=Status.EXPIRED, updated_at=now)
    require_open(p, now)
    if to_status == Status.DECLINED:
        if p.status != Status.APPROVAL_REQUESTED or p.approval_function_call_id != function_call_id:
            raise ProposalError("approval_mismatch", "Not the active approval request.")
    elif to_status != Status.CANCELLED:
        raise ValueError(to_status)
    return replace(p, status=to_status, updated_at=now)


def _request_rules(p: Proposal, function_call_id: str, now: datetime) -> Proposal:
    if p.status == Status.EXECUTED:
        return p
    require_open(p, now)
    return replace(
        p,
        status=Status.APPROVAL_REQUESTED,
        approval_function_call_id=function_call_id,
        approval_requested_at=now,
        updated_at=now,
    )


# --- In-memory repository (offline tests; same rules, lock = transaction) -----


class InMemoryProposalRepository:
    def __init__(self, work_orders: list[dict], next_work_order_number: int | None = None):
        self._lock = threading.RLock()
        self.proposals: dict[str, Proposal] = {}
        self.executions: dict[str, dict] = {}
        self.work_orders: dict[str, dict] = {wo["work_order_id"]: dict(wo) for wo in work_orders}
        self.counter = next_work_order_number or (
            max((int(w.split("-")[1]) for w in self.work_orders), default=0) + 1
        )
        self.fail_after_commit = 0  # tests: raise TimeoutError after N committed executions

    def create_superseding(self, p: Proposal, now: datetime) -> None:
        with self._lock:
            for other in list(self.proposals.values()):
                if other.owner_subject == p.owner_subject and other.status in OPEN:
                    self.proposals[other.proposal_id] = replace(
                        other, status=Status.SUPERSEDED, superseded_by=p.proposal_id, updated_at=now
                    )
            self.proposals[p.proposal_id] = p

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            return self.proposals.get(proposal_id)

    def list_for_owner(self, owner_subject: str) -> list[Proposal]:
        with self._lock:
            return [p for p in self.proposals.values() if p.owner_subject == owner_subject]

    def request_approval(self, proposal_id, auth, function_call_id, now):
        with self._lock:
            p = check_owner(self.proposals.get(proposal_id), auth)
            updated = _request_rules(p, function_call_id, now)
            self.proposals[proposal_id] = updated
            return updated

    def close(self, proposal_id, auth, to_status, now, function_call_id=None):
        with self._lock:
            p = check_owner(self.proposals.get(proposal_id), auth)
            updated = _close_rules(p, to_status, now, function_call_id)
            self.proposals[proposal_id] = updated
            return updated

    def execute(self, proposal_id, auth, function_call_id, now):
        with self._lock:
            p = check_owner(self.proposals.get(proposal_id), auth)
            done = self.executions.get(p.execution_key_value)
            if done:
                return ExecutionResult(done["work_order_id"], p.proposal_id, False)
            check_executable(p, function_call_id, now)
            unit_orders = [w for w in self.work_orders.values() if w["unit_id"] == p.unit_id]
            if blockers := duplicate_blockers(p, unit_orders):
                raise possible_duplicate(blockers)
            wo_id = format_work_order_id(self.counter)
            if wo_id in self.work_orders:
                raise ProposalError("conflict", "Work-order ID already in use.")
            wo = build_work_order(p, wo_id, now)
            self.work_orders[wo_id] = wo
            self.counter += 1
            self.executions[p.execution_key_value] = {
                "work_order_id": wo_id,
                "proposal_id": p.proposal_id,
            }
            self.proposals[proposal_id] = replace(
                p,
                status=Status.EXECUTED,
                work_order_id=wo_id,
                execution_key=p.execution_key_value,
                updated_at=now,
            )
            if self.fail_after_commit:
                self.fail_after_commit -= 1
                raise TimeoutError("simulated timeout after commit")
            return ExecutionResult(wo_id, p.proposal_id, True, wo)


# --- Firestore repository ------------------------------------------------------


class FirestoreProposalRepository:
    """All state changes are Firestore transactions. Transaction callbacks are
    pure: they only read/write Firestore and evaluate the rules above (no model
    calls, no logging side effects), because Firestore may re-run them."""

    def __init__(
        self,
        client,
        *,
        max_attempts: int = 8,
        outer_retries: int = 6,
        backoff: Callable[[int], float] = lambda i: (
            min(0.05 * 2**i, 1.0) * random.uniform(0.5, 1.5)
        ),
    ):
        self._db = client
        self._max_attempts = max_attempts
        self._outer_retries = outer_retries
        self._backoff = backoff

    def _ref(self, collection: str, doc_id: str):
        return self._db.collection(collection).document(doc_id)

    def _run(self, fn):
        """Run ``fn(transaction)`` transactionally, retrying contention with backoff."""
        from google.api_core import exceptions as gexc
        from google.cloud import firestore

        last: Exception | None = None
        for attempt in range(self._outer_retries):
            try:
                return firestore.transactional(fn)(
                    self._db.transaction(max_attempts=self._max_attempts)
                )
            except ProposalError:
                raise
            except gexc.AlreadyExists as e:
                # Permanent (e.g. counter behind existing IDs): retrying cannot help.
                raise ProposalError(
                    "conflict", "A record already exists with that ID; nothing was created."
                ) from e
            except (gexc.Aborted, gexc.Conflict, gexc.DeadlineExceeded) as e:
                last = e
            except ValueError as e:  # "Failed to commit transaction in N attempts."
                if "Failed to commit transaction" not in str(e):
                    raise
                last = e
            time.sleep(self._backoff(attempt))
        raise ProposalError(
            "busy", "The system is busy; the request was not completed. Try again."
        ) from last

    def _read(self, tx, proposal_id: str) -> Proposal | None:
        snap = self._ref(PROPOSALS, proposal_id).get(transaction=tx)
        return Proposal.from_doc(snap.to_dict()) if snap.exists else None

    def create_superseding(self, p: Proposal, now: datetime) -> None:
        from google.cloud.firestore_v1.base_query import FieldFilter

        def fn(tx):
            query = self._db.collection(PROPOSALS).where(
                filter=FieldFilter("owner_subject", "==", p.owner_subject)
            )
            mine = list(query.stream(transaction=tx))
            for snap in mine:
                if snap.get("status") in {s.value for s in OPEN}:
                    tx.update(
                        snap.reference,
                        {
                            "status": Status.SUPERSEDED.value,
                            "superseded_by": p.proposal_id,
                            "updated_at": now,
                        },
                    )
            tx.create(self._ref(PROPOSALS, p.proposal_id), p.to_doc())

        self._run(fn)

    def get(self, proposal_id: str) -> Proposal | None:
        snap = self._ref(PROPOSALS, proposal_id).get()
        return Proposal.from_doc(snap.to_dict()) if snap.exists else None

    def list_for_owner(self, owner_subject: str) -> list[Proposal]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._db.collection(PROPOSALS).where(
            filter=FieldFilter("owner_subject", "==", owner_subject)
        )
        return [Proposal.from_doc(s.to_dict()) for s in query.stream()]

    def request_approval(self, proposal_id, auth, function_call_id, now):
        def fn(tx):
            p = check_owner(self._read(tx, proposal_id), auth)
            updated = _request_rules(p, function_call_id, now)
            if updated is not p:
                tx.set(self._ref(PROPOSALS, proposal_id), updated.to_doc())
            return updated

        return self._run(fn)

    def close(self, proposal_id, auth, to_status, now, function_call_id=None):
        def fn(tx):
            p = check_owner(self._read(tx, proposal_id), auth)
            updated = _close_rules(p, to_status, now, function_call_id)
            if updated is not p:
                tx.set(self._ref(PROPOSALS, proposal_id), updated.to_doc())
            return updated

        return self._run(fn)

    def execute(self, proposal_id, auth, function_call_id, now):
        from google.cloud.firestore_v1.base_query import FieldFilter

        def fn(tx):
            # Reads first (Firestore requires all reads before writes).
            p = check_owner(self._read(tx, proposal_id), auth)
            exec_ref = self._ref(EXECUTIONS, p.execution_key_value)
            done = exec_ref.get(transaction=tx)
            if done.exists:
                return ExecutionResult(done.get("work_order_id"), p.proposal_id, False)
            check_executable(p, function_call_id, now)
            unit_orders = [
                s.to_dict()
                for s in self._db.collection("work_orders")
                .where(filter=FieldFilter("unit_id", "==", p.unit_id))
                .stream(transaction=tx)
            ]
            if blockers := duplicate_blockers(p, unit_orders):
                raise possible_duplicate(blockers)
            counter_ref = self._ref(COUNTERS, WORK_ORDER_COUNTER)
            counter = counter_ref.get(transaction=tx)
            if not counter.exists:
                raise ProposalError("unavailable", "Work-order numbering is not initialized.")
            n = int(counter.get("next"))
            wo_id = format_work_order_id(n)
            wo = build_work_order(p, wo_id, now)

            # Writes: all-or-nothing.
            tx.create(self._ref("work_orders", wo_id), wo)
            tx.update(counter_ref, {"next": n + 1})
            tx.create(
                exec_ref,
                {
                    "execution_key": p.execution_key_value,
                    "proposal_id": p.proposal_id,
                    "payload_hash": p.payload_hash,
                    "work_order_id": wo_id,
                    "owner_subject": p.owner_subject,
                    "created_at": now,
                },
            )
            tx.update(
                self._ref(PROPOSALS, proposal_id),
                {
                    "status": Status.EXECUTED.value,
                    "work_order_id": wo_id,
                    "execution_key": p.execution_key_value,
                    "updated_at": now,
                },
            )
            return ExecutionResult(wo_id, p.proposal_id, True, wo)

        return self._run(fn)


# --- Service (used by tools) ---------------------------------------------------


class SectionCatalog(Protocol):
    def published_sections(self, property_id: str, section_ids: list[str]) -> set[str]: ...


class ProposalService:
    def __init__(
        self,
        repo: ProposalRepository,
        work_orders: WorkOrderRepository,
        sections: SectionCatalog,
        clock: Clock,
        ttl_seconds: int,
        id_factory: Callable[[], str] = lambda: f"PROPOSAL-{uuid.uuid4().hex[:12].upper()}",
    ):
        self.repo, self.work_orders, self.sections = repo, work_orders, sections
        self.clock, self.ttl, self.new_id = clock, timedelta(seconds=ttl_seconds), id_factory

    def prepare(
        self,
        auth: AuthContext,
        content: ProposalContent,
        acknowledged_work_order_ids: Iterable[str] = (),
    ) -> Proposal:
        resident_id, property_id, unit_id = resident_scope(auth)
        content.validate()

        cited = list(dict.fromkeys(content.policy_section_ids))
        if not cited:
            raise ProposalError(
                "policy_required",
                "Cite the policy section(s) used to set the priority (search_policies first).",
            )
        valid = self.sections.published_sections(property_id, cited)
        if unknown := [s for s in cited if s not in valid]:
            raise ProposalError(
                "invalid_policy_sections",
                "These sections are not published policies for the resident's property.",
                section_ids=unknown,
            )

        unit_orders = [
            wo for wo in self.work_orders.list_for_units([unit_id]) if wo.get("unit_id") == unit_id
        ]
        matches = active_matches(unit_orders, content.category)
        match_ids = {wo["work_order_id"] for wo in matches}
        acknowledged = tuple(sorted(match_ids & set(acknowledged_work_order_ids)))
        if unacknowledged := [wo for wo in matches if wo["work_order_id"] not in acknowledged]:
            raise possible_duplicate(unacknowledged)

        now = self.clock.now()
        p = Proposal(
            proposal_id=self.new_id(),
            owner_subject=auth.subject,
            resident_id=resident_id,
            property_id=property_id,
            unit_id=unit_id,
            content=replace(content, policy_section_ids=tuple(cited)),
            created_at=now,
            expires_at=now + self.ttl,
            acknowledged_work_order_ids=acknowledged,
            updated_at=now,
        ).sealed()
        self.repo.create_superseding(p, now)
        return p

    def get(self, auth: AuthContext, proposal_id: str) -> Proposal:
        return check_owner(self.repo.get(proposal_id), auth)

    def recent(self, auth: AuthContext, limit: int = 5) -> list[Proposal]:
        """The actor's own pending, expired or executed proposals, newest first.
        Superseded/cancelled/declined ones are omitted to avoid ambiguity."""
        now = self.clock.now()
        mine = []
        for p in self.repo.list_for_owner(auth.subject):
            try:
                check_owner(p, auth)
            except ProposalError:
                continue
            if effective_status(p, now) in (*OPEN, Status.EXPIRED, Status.EXECUTED):
                mine.append(p)
        mine.sort(key=lambda p: p.created_at, reverse=True)
        return mine[:limit]

    def request_approval(self, auth: AuthContext, proposal_id: str, function_call_id: str):
        try:
            return self.repo.request_approval(proposal_id, auth, function_call_id, self.clock.now())
        except ProposalError as e:
            if e.kind == "expired":
                self.repo.close(proposal_id, auth, Status.EXPIRED, self.clock.now())
            raise

    def decline(self, auth: AuthContext, proposal_id: str, function_call_id: str) -> Proposal:
        return self.repo.close(
            proposal_id, auth, Status.DECLINED, self.clock.now(), function_call_id
        )

    def cancel(self, auth: AuthContext, proposal_id: str) -> Proposal:
        return self.repo.close(proposal_id, auth, Status.CANCELLED, self.clock.now())

    def execute(self, auth: AuthContext, proposal_id: str, function_call_id: str):
        now = self.clock.now()  # computed outside the transaction
        try:
            return self.repo.execute(proposal_id, auth, function_call_id, now)
        except ProposalError as e:
            if e.kind == "expired":
                self.repo.close(proposal_id, auth, Status.EXPIRED, now)
            raise


# --- Presentation (allowlisted; backend-rendered) ------------------------------

PROPOSAL_VIEW_FIELDS = (
    "proposal_id",
    "status",
    "category",
    "summary",
    "description",
    "priority",
    "entry_permission",
    "policy_section_ids",
    "acknowledged_work_order_ids",
    "expires_at",
    "work_order_id",
)

_ENTRY_TEXT = {
    "permission_granted": "Permission to enter granted",
    "appointment_required": "Appointment required before entry",
}


def proposal_view(p: Proposal, now: datetime) -> dict:
    c = p.content
    values = {
        "proposal_id": p.proposal_id,
        "status": str(effective_status(p, now)),
        "category": c.category,
        "summary": c.summary,
        "description": c.description,
        "priority": c.priority,
        "entry_permission": c.entry_permission,
        "policy_section_ids": list(c.policy_section_ids),
        "acknowledged_work_order_ids": list(p.acknowledged_work_order_ids),
        "expires_at": p.expires_at.isoformat(),
        "work_order_id": p.work_order_id,
    }
    return {k: values[k] for k in PROPOSAL_VIEW_FIELDS}


def render_for_approval(p: Proposal) -> str:
    """Exact contents shown in the confirmation dialog (rendered by the backend)."""
    c = p.content
    lines = [
        f"Create this maintenance request? (proposal {p.proposal_id})",
        f"Category: {c.category}",
        f"Priority: {c.priority}",
        f"Summary: {c.summary}",
        f"Description: {c.description}",
        f"Entry: {_ENTRY_TEXT[c.entry_permission]}",
    ]
    if p.acknowledged_work_order_ids:
        lines.append(
            "Separate from existing request(s): " + ", ".join(p.acknowledged_work_order_ids)
        )
    lines += [
        f"Policy basis: {', '.join(c.policy_section_ids)}",
        f"Approval expires: {p.expires_at.isoformat()}",
        f"Fingerprint: {p.payload_hash[:12]}",
    ]
    return "\n".join(lines)
