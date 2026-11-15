"""Transaction and concurrency tests against the Firestore emulator."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.proposals import (
    COUNTERS,
    EXECUTIONS,
    PROPOSALS,
    WORK_ORDER_COUNTER,
    Proposal,
    ProposalContent,
    ProposalError,
    Status,
)
from app.demo_auth import load_demo_context

RES = {i: load_demo_context(f"AUTH-RES-00{i}") for i in range(1, 6)}
SECTION = {"PROP-001": "DOC-CG-WORKORDER-S01", "PROP-002": "DOC-LV-WORKORDER-S01"}


def content(auth, category="electrical", **kw):
    prop = next(iter(auth.allowed_property_ids))
    base = dict(
        category=category,
        summary="Outlet not working",
        description="Kitchen outlet has no power.",
        priority="routine",
        entry_permission="permission_granted",
        policy_section_ids=(SECTION[prop],),
    )
    return ProposalContent(**{**base, **kw})


def approved(svc, auth, **kw):
    p = svc.prepare(auth, content(auth, **kw))
    svc.request_approval(auth, p.proposal_id, "fc-1")
    return p


def count(db, collection):
    return len(list(db.collection(collection).list_documents()))


def counter(db):
    return db.collection(COUNTERS).document(WORK_ORDER_COUNTER).get().get("next")


def test_lifecycle_persists_atomically(db, service):
    svc = service()
    p = approved(svc, RES[2])
    stored = svc.repo.get(p.proposal_id)
    assert stored.status == Status.APPROVAL_REQUESTED
    assert stored.payload_hash == stored.compute_hash()  # survives Firestore round trip

    result = svc.execute(RES[2], p.proposal_id, "fc-1")
    assert (result.work_order_id, result.created) == ("WO-011", True)
    wo = db.collection("work_orders").document("WO-011").get().to_dict()
    assert (wo["unit_id"], wo["resident_id"], wo["status"]) == ("UNIT-002", "RES-002", "open")
    assert counter(db) == 12
    ex = db.collection(EXECUTIONS).document(p.execution_key_value).get().to_dict()
    assert ex["work_order_id"] == "WO-011" and ex["proposal_id"] == p.proposal_id
    assert svc.repo.get(p.proposal_id).status == Status.EXECUTED


def test_concurrent_execution_of_one_proposal_creates_exactly_one(db, service):
    svc = service()
    p = approved(svc, RES[2])
    barrier = threading.Barrier(10)

    def attempt():
        barrier.wait()
        try:
            return svc.execute(RES[2], p.proposal_id, "fc-1").work_order_id
        except ProposalError as e:
            assert e.kind == "busy"  # contention exhausted retries: nothing half-done
            return None

    with ThreadPoolExecutor(10) as pool:
        ids = list(pool.map(lambda _: attempt(), range(10)))
    assert {i for i in ids if i} == {"WO-011"}
    # Any caller that got "busy" recovers the same ID on retry.
    assert svc.execute(RES[2], p.proposal_id, "fc-1").work_order_id == "WO-011"
    assert count(db, "work_orders") == 11 and counter(db) == 12
    assert count(db, EXECUTIONS) == 1


def test_counter_has_no_gaps_or_duplicates_under_contention(db, service):
    svc = service()
    proposals = {i: approved(svc, RES[i]) for i in range(1, 6)}
    barrier = threading.Barrier(5)

    def run(i):
        barrier.wait()
        for _ in range(5):
            try:
                return svc.execute(RES[i], proposals[i].proposal_id, "fc-1").work_order_id
            except ProposalError as e:
                assert e.kind == "busy"
        raise AssertionError("never succeeded")

    with ThreadPoolExecutor(5) as pool:
        ids = sorted(pool.map(run, range(1, 6)))
    assert ids == ["WO-011", "WO-012", "WO-013", "WO-014", "WO-015"]
    assert counter(db) == 16


def test_racing_separate_proposals_for_same_issue_create_one(db, service, clock):
    """Two approved proposals for the same unit+category (as if both were
    prepared before either executed): the in-transaction duplicate check
    lets exactly one through."""
    svc = service()
    first = approved(svc, RES[2], category="hvac")
    # Bypass supersession to model the race the transaction must still catch.
    twin = Proposal(
        proposal_id="PROPOSAL-TWIN",
        owner_subject=first.owner_subject,
        resident_id=first.resident_id,
        property_id=first.property_id,
        unit_id=first.unit_id,
        content=first.content,
        created_at=first.created_at,
        expires_at=first.expires_at,
        status=Status.APPROVAL_REQUESTED,
        approval_function_call_id="fc-1",
    ).sealed()
    db.collection(PROPOSALS).document(twin.proposal_id).set(twin.to_doc())

    barrier = threading.Barrier(2)
    outcomes = {}

    def run(pid):
        barrier.wait()
        for _ in range(5):
            try:
                outcomes[pid] = svc.execute(RES[2], pid, "fc-1").work_order_id
                return
            except ProposalError as e:
                outcomes[pid] = e.kind
                if e.kind != "busy":
                    return

    threads = [
        threading.Thread(target=run, args=(pid,)) for pid in (first.proposal_id, "PROPOSAL-TWIN")
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes.values()) == ["WO-011", "possible_duplicate"]
    assert count(db, "work_orders") == 11


def test_failed_commit_rolls_back_everything(db, service):
    """A write conflict inside the transaction leaves no partial state."""
    svc = service()
    p = approved(svc, RES[2])
    db.collection("work_orders").document("WO-011").set(
        {
            "work_order_id": "WO-011",
            "unit_id": "UNIT-005",
            "category": "access",
            "status": "completed",
        }
    )
    with pytest.raises(ProposalError) as e:
        svc.execute(RES[2], p.proposal_id, "fc-1")
    assert e.value.kind == "conflict"
    assert counter(db) == 11
    assert count(db, EXECUTIONS) == 0
    assert svc.repo.get(p.proposal_id).status == Status.APPROVAL_REQUESTED


def test_timeout_after_commit_is_recoverable_via_tool(db, service):
    """The tool times out while the transaction commits in the background; a
    retry returns the same work order and never creates a second one."""
    from tests.agent_fakes import FakeToolContext, confirmed, make_creation_tools

    svc = service()
    p = approved(svc, RES[2])
    gate = threading.Event()
    real_execute = svc.repo.execute

    def slow_execute(*a, **kw):
        result = real_execute(*a, **kw)  # commits
        gate.wait(5)  # ...but the reply is late
        return result

    svc.repo.execute = slow_execute
    t = make_creation_tools("AUTH-RES-002", service=svc, timeout=0.5)
    result = asyncio.run(t.submit(p.proposal_id, FakeToolContext(confirmed(True), "fc-1")))
    assert result["error"] == "outcome_unknown"
    gate.set()
    svc.repo.execute = real_execute

    retry = asyncio.run(t.submit(p.proposal_id, FakeToolContext()))
    assert retry["status"] == "executed" and retry["work_order_id"] == "WO-011"
    assert retry["newly_created"] is False
    assert count(db, "work_orders") == 11


def test_rules_enforced_inside_transaction(db, service, clock):
    svc = service(ttl=300)
    p = approved(svc, RES[2])
    with pytest.raises(ProposalError) as e:
        svc.execute(RES[1], p.proposal_id, "fc-1")
    assert e.value.kind == "not_found"
    with pytest.raises(ProposalError) as e:
        svc.execute(RES[2], p.proposal_id, "fc-other")
    assert e.value.kind == "approval_mismatch"
    clock.advance(seconds=301)
    with pytest.raises(ProposalError) as e:
        svc.execute(RES[2], p.proposal_id, "fc-1")
    assert e.value.kind == "expired"
    assert svc.repo.get(p.proposal_id).status == Status.EXPIRED
    assert count(db, "work_orders") == 10


def test_supersede_is_atomic_with_create(db, service):
    svc = service()
    a = svc.prepare(RES[2], content(RES[2]))
    b = svc.prepare(RES[2], content(RES[2], priority="urgent"))
    statuses = {d.id: d.get("status") for d in db.collection(PROPOSALS).stream()}
    assert statuses == {a.proposal_id: "superseded", b.proposal_id: "awaiting_approval"}
