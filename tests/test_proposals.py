"""Offline tests for the proposal service and rules (in-memory repository).

Atomicity and concurrency against real Firestore are covered by
tests/emulator/; these tests pin down the rules both repositories share.
"""

import threading
from dataclasses import replace

import pytest

from app.agent.proposals import (
    ProposalContent,
    ProposalError,
    Status,
    render_for_approval,
)
from app.demo_auth import load_demo_context
from tests.agent_fakes import make_service

RES1 = load_demo_context("AUTH-RES-001")  # PROP-001 / UNIT-001, active hvac WO-001
RES2 = load_demo_context("AUTH-RES-002")  # PROP-001 / UNIT-002, completed hvac WO-004
RES3 = load_demo_context("AUTH-RES-003")  # PROP-001 / UNIT-003, in-progress plumbing WO-005
RES4 = load_demo_context("AUTH-RES-004")  # PROP-002 / UNIT-004
MGR = load_demo_context("AUTH-MGR-001")


def hvac(**kw):
    base = dict(
        category="hvac",
        summary="AC not cooling",
        description="AC stopped; indoor temperature is 80 F.",
        priority="routine",
        entry_permission="appointment_required",
        policy_section_ids=("DOC-CG-HVAC-S01",),
        reported_facts="indoor temperature 80 F",
    )
    return ProposalContent(**{**base, **kw})


def kind(fn):
    with pytest.raises(ProposalError) as e:
        fn()
    return e.value.kind


def approved(svc, auth, p, fc="fc-1"):
    svc.request_approval(auth, p.proposal_id, fc)
    return fc


# --- Prepare: scope, validation, policy basis -----------------------------------


def test_prepare_binds_owner_from_identity():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    assert (p.owner_subject, p.resident_id, p.property_id, p.unit_id) == (
        "demo-resident-002",
        "RES-002",
        "PROP-001",
        "UNIT-002",
    )
    assert p.status == Status.AWAITING_APPROVAL
    assert p.payload_hash == p.compute_hash() and len(p.payload_hash) == 64
    assert (p.expires_at - p.created_at).total_seconds() == 900
    assert repo.work_orders.keys().isdisjoint({"WO-011"})  # nothing created


def test_ttl_is_configurable():
    svc, _, _ = make_service(ttl=120)
    p = svc.prepare(RES2, hvac())
    assert (p.expires_at - p.created_at).total_seconds() == 120


def test_managers_cannot_create():
    svc, _, _ = make_service()
    assert kind(lambda: svc.prepare(MGR, hvac())) == "forbidden"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "roof"),
        ("priority", "asap"),
        ("entry_permission", ""),
        ("entry_permission", "yes"),
        ("summary", ""),
        ("description", "   "),
    ],
)
def test_invalid_fields(field, value):
    svc, _, _ = make_service()
    assert kind(lambda: svc.prepare(RES2, hvac(**{field: value}))) == "invalid_field"


def test_policy_basis_required_and_must_be_own_property():
    svc, _, _ = make_service()
    assert kind(lambda: svc.prepare(RES2, hvac(policy_section_ids=()))) == "policy_required"
    lakeview = hvac(policy_section_ids=("DOC-LV-HVAC-S01",))
    assert kind(lambda: svc.prepare(RES2, lakeview)) == "invalid_policy_sections"
    assert kind(lambda: svc.prepare(RES2, hvac(policy_section_ids=("DOC-FAKE-S01",)))) == (
        "invalid_policy_sections"
    )


# --- Duplicates -------------------------------------------------------------------


def test_active_same_category_requires_clarification():
    svc, _, _ = make_service()
    with pytest.raises(ProposalError) as e:
        svc.prepare(RES1, hvac())
    assert e.value.kind == "possible_duplicate"
    [match] = e.value.extra["matching_work_orders"]
    assert match["work_order_id"] == "WO-001" and "internal_notes" not in match


def test_acknowledged_different_issue_allowed():
    svc, _, _ = make_service()
    p = svc.prepare(RES1, hvac(), acknowledged_work_order_ids=["WO-001", "WO-999"])
    assert p.acknowledged_work_order_ids == ("WO-001",)  # only real matches are recorded


def test_completed_and_cancelled_do_not_block():
    svc, _, _ = make_service()
    svc.prepare(RES2, hvac())  # WO-004 hvac is completed
    access = hvac(category="access", summary="Door lock", description="Front door lock sticks.")
    svc.prepare(RES3, access)  # WO-006 access is cancelled


def test_in_progress_blocks_and_other_categories_do_not():
    svc, _, _ = make_service()
    plumbing = hvac(category="plumbing", summary="Leak", description="Sink leaks.")
    assert kind(lambda: svc.prepare(RES3, plumbing)) == "possible_duplicate"  # WO-005
    svc.prepare(RES3, hvac())  # no hvac orders for UNIT-003


def test_execution_rechecks_duplicates_created_after_prepare():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    repo.work_orders["WO-050"] = {
        "work_order_id": "WO-050",
        "unit_id": "UNIT-002",
        "property_id": "PROP-001",
        "resident_id": "RES-002",
        "category": "hvac",
        "status": "open",
    }
    assert kind(lambda: svc.execute(RES2, p.proposal_id, fc)) == "possible_duplicate"
    assert "WO-011" not in repo.work_orders


# --- Ownership, supersession, expiry, integrity -----------------------------------


@pytest.mark.parametrize("other", [RES1, RES4, MGR])
def test_foreign_proposals_are_indistinguishable_from_missing(other):
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    for op in (
        lambda: svc.request_approval(other, p.proposal_id, "x"),
        lambda: svc.execute(other, p.proposal_id, fc),
        lambda: svc.cancel(other, p.proposal_id),
        lambda: svc.get(other, p.proposal_id),
    ):
        assert kind(op) == "not_found"
    assert kind(lambda: svc.execute(RES2, "PROPOSAL-NOPE", fc)) == "not_found"


def test_new_prepare_supersedes_previous_open_proposal():
    svc, repo, _ = make_service()
    first = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, first)
    second = svc.prepare(RES2, hvac(priority="urgent", reported_facts="indoor 90 F"))
    assert repo.get(first.proposal_id).status == Status.SUPERSEDED
    assert repo.get(first.proposal_id).superseded_by == second.proposal_id
    assert kind(lambda: svc.execute(RES2, first.proposal_id, fc)) == "superseded"
    # The old approval does not carry over to the new proposal.
    assert kind(lambda: svc.execute(RES2, second.proposal_id, fc)) == "approval_missing"


def test_expired_proposal_rejected_and_marked():
    svc, repo, clock = make_service(ttl=600)
    p = svc.prepare(RES2, hvac())
    clock.advance(seconds=601)
    assert kind(lambda: svc.request_approval(RES2, p.proposal_id, "fc")) == "expired"
    assert repo.get(p.proposal_id).status == Status.EXPIRED


def test_expiry_checked_at_execution():
    svc, repo, clock = make_service(ttl=600)
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    clock.advance(seconds=600)
    assert kind(lambda: svc.execute(RES2, p.proposal_id, fc)) == "expired"
    assert "WO-011" not in repo.work_orders


def test_stored_payload_tampering_detected():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    stored = repo.proposals[p.proposal_id]
    repo.proposals[p.proposal_id] = replace(
        stored, content=replace(stored.content, priority="emergency")
    )
    assert kind(lambda: svc.execute(RES2, p.proposal_id, fc)) == "integrity"


def test_any_content_change_changes_hash():
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    for change in (
        dict(priority="urgent"),
        dict(entry_permission="permission_granted"),
        dict(description="different"),
        dict(policy_section_ids=("DOC-CG-HVAC-S03",)),
    ):
        changed = replace(p, content=replace(p.content, **change))
        assert changed.compute_hash() != p.payload_hash
    assert replace(p, unit_id="UNIT-004").compute_hash() != p.payload_hash


def test_cancelled_proposal_cannot_be_approved():
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    svc.cancel(RES2, p.proposal_id)
    assert kind(lambda: svc.request_approval(RES2, p.proposal_id, "fc")) == "cancelled"


# --- Approval binding ---------------------------------------------------------------


def test_execution_requires_approval_request():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    assert kind(lambda: svc.execute(RES2, p.proposal_id, "fc-any")) == "approval_missing"
    assert len(repo.work_orders) == 10


def test_approval_bound_to_latest_request():
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    approved(svc, RES2, p, "fc-old")
    approved(svc, RES2, p, "fc-new")
    assert kind(lambda: svc.execute(RES2, p.proposal_id, "fc-old")) == "approval_mismatch"
    assert kind(lambda: svc.execute(RES2, p.proposal_id, None)) == "approval_mismatch"
    assert svc.execute(RES2, p.proposal_id, "fc-new").created


def test_declined_is_terminal():
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    svc.decline(RES2, p.proposal_id, fc)
    assert kind(lambda: svc.execute(RES2, p.proposal_id, fc)) == "declined"


# --- Execution: idempotency, recovery, record shape -----------------------------------


def test_execute_creates_once_and_replays_same_id():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    first = svc.execute(RES2, p.proposal_id, fc)
    second = svc.execute(RES2, p.proposal_id, fc)
    assert (first.work_order_id, first.created) == ("WO-011", True)
    assert (second.work_order_id, second.created) == ("WO-011", False)
    assert len(repo.work_orders) == 11 and repo.counter == 12
    wo = repo.work_orders["WO-011"]
    assert (wo["unit_id"], wo["property_id"], wo["resident_id"]) == (
        "UNIT-002",
        "PROP-001",
        "RES-002",
    )
    assert wo["status"] == "open" and wo["scheduled_window"] is None
    assert wo["entry_permission"] == "appointment_required"
    assert repo.get(p.proposal_id).status == Status.EXECUTED


def test_timeout_after_commit_recovers_same_id():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    repo.fail_after_commit = 1
    with pytest.raises(TimeoutError):
        svc.execute(RES2, p.proposal_id, fc)
    retry = svc.execute(RES2, p.proposal_id, fc)
    assert (retry.work_order_id, retry.created) == ("WO-011", False)
    assert len(repo.work_orders) == 11


def test_concurrent_execution_in_memory_creates_one():
    svc, repo, _ = make_service()
    p = svc.prepare(RES2, hvac())
    fc = approved(svc, RES2, p)
    ids = []
    threads = [
        threading.Thread(
            target=lambda: ids.append(svc.execute(RES2, p.proposal_id, fc).work_order_id)
        )
        for _ in range(8)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert set(ids) == {"WO-011"} and len(ids) == 8 and len(repo.work_orders) == 11


def test_approval_display_shows_exact_contents_and_entry_separately():
    svc, _, _ = make_service()
    p = svc.prepare(RES2, hvac())
    text = render_for_approval(p)
    for expected in (
        p.proposal_id,
        "Category: hvac",
        "Priority: routine",
        "Summary: AC not cooling",
        "Description: AC stopped; indoor temperature is 80 F.",
        "Entry: Appointment required before entry",
        "DOC-CG-HVAC-S01",
        p.payload_hash[:12],
    ):
        assert expected in text
