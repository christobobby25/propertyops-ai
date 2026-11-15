"""Offline tests for the creation tools: approval boundary, recovery, wiring."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.factory import build_agent
from app.agent.guards import make_before_tool_callback
from app.agent.identity import StaticIdentityProvider
from app.agent.prompt import INSTRUCTION, INSTRUCTION_WITH_CREATION
from app.config import get_settings
from tests.agent_fakes import FakeToolContext, confirmed, make_creation_tools, make_service

ARGS = dict(
    category="hvac",
    summary="AC not cooling",
    description="AC stopped; indoor temperature is 80 F.",
    priority="routine",
    entry_permission="appointment_required",
    policy_section_ids=["DOC-CG-HVAC-S01"],
    reported_facts="indoor temperature 80 F",
)


def run(coro):
    return asyncio.run(coro)


def prepared(t, **overrides):
    result = run(t.prepare(**{**ARGS, **overrides}, tool_context=FakeToolContext()))
    assert result["status"] == "ok", result
    return result["proposal"]["proposal_id"]


def request(t, pid):
    ctx = FakeToolContext()
    result = run(t.submit(pid, ctx))
    return ctx, result


def test_prepare_never_creates_and_returns_exact_display():
    t = make_creation_tools()
    result = run(t.prepare(**ARGS, tool_context=FakeToolContext()))
    assert result["status"] == "ok"
    assert "Entry: Appointment required before entry" in result["display"]
    assert set(result["proposal"]) >= {"proposal_id", "status", "priority", "entry_permission"}
    assert {"resident_id", "unit_id", "property_id"}.isdisjoint(result["proposal"])
    assert len(t.svc.repo.work_orders) == 10


def test_first_submit_only_requests_confirmation():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, result = request(t, pid)
    assert result["status"] == "awaiting_confirmation"
    assert ctx.requested["payload"]["proposal_id"] == pid
    assert ctx.requested["payload"]["payload_hash"] == t.svc.repo.get(pid).payload_hash
    assert "Priority: routine" in ctx.requested["hint"]
    assert len(t.svc.repo.work_orders) == 10


def test_confirmed_resume_creates_and_returns_id():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, _ = request(t, pid)
    resumed = FakeToolContext(confirmed(True), function_call_id=ctx.function_call_id)
    result = run(t.submit(pid, resumed))
    assert result["status"] == "executed" and result["work_order_id"] == "WO-011"
    assert result["newly_created"] is True
    assert "internal_notes" not in json.dumps(result)
    assert "not been scheduled or dispatched" in result["message"]


def test_declined_confirmation_creates_nothing():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, _ = request(t, pid)
    result = run(t.submit(pid, FakeToolContext(confirmed(False), ctx.function_call_id)))
    assert result["status"] == "declined"
    assert len(t.svc.repo.work_orders) == 10


def test_confirmation_for_a_different_call_is_rejected():
    t = make_creation_tools()
    pid = prepared(t)
    request(t, pid)
    forged = FakeToolContext(confirmed(True), function_call_id="call-not-the-request")
    assert run(t.submit(pid, forged))["error"] == "approval_mismatch"
    assert len(t.svc.repo.work_orders) == 10


def test_retry_after_execution_returns_same_id_without_new_confirmation():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, _ = request(t, pid)
    run(t.submit(pid, FakeToolContext(confirmed(True), ctx.function_call_id)))
    again_ctx, again = request(t, pid)
    assert again["status"] == "executed" and again["work_order_id"] == "WO-011"
    assert again["newly_created"] is False and again_ctx.requested is None
    assert len(t.svc.repo.work_orders) == 11


def test_timeout_after_commit_reports_unknown_then_recovers():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, _ = request(t, pid)
    t.svc.repo.fail_after_commit = 1
    result = run(t.submit(pid, FakeToolContext(confirmed(True), ctx.function_call_id)))
    assert result["error"] == "outcome_unknown"
    assert "never creates a second one" in result["message"] or "safe" in result["message"]
    _, recovered = request(t, pid)
    assert recovered["work_order_id"] == "WO-011" and len(t.svc.repo.work_orders) == 11


def test_slow_backend_times_out_as_unknown():
    import time

    class Slow:
        clock = make_service()[0].clock

        def request_approval(self, *a):
            time.sleep(1)

    t = make_creation_tools(service=Slow(), timeout=0.05)
    result = run(t.submit("PROPOSAL-X", FakeToolContext()))
    assert result["error"] == "outcome_unknown"


def test_errors_are_structured():
    t = make_creation_tools()
    bad = run(t.prepare(**{**ARGS, "entry_permission": ""}, tool_context=FakeToolContext()))
    assert bad["error"] == "invalid_field" and bad["field"] == "entry_permission"
    dup = run(make_creation_tools("AUTH-RES-001").prepare(**ARGS, tool_context=FakeToolContext()))
    assert dup["error"] == "possible_duplicate"
    assert [w["work_order_id"] for w in dup["matching_work_orders"]] == ["WO-001"]


def test_cross_resident_submit_and_cancel_rejected():
    svc = make_service()[0]
    owner = make_creation_tools("AUTH-RES-002", service=svc)
    pid = prepared(owner)
    intruder = make_creation_tools("AUTH-RES-001", service=svc)
    assert run(intruder.submit(pid, FakeToolContext()))["error"] == "not_found"
    assert run(intruder.cancel(pid, FakeToolContext()))["error"] == "not_found"


def test_missing_identity_fails_closed():
    t = make_creation_tools(None, identity=StaticIdentityProvider(None))
    assert run(t.prepare(**ARGS, tool_context=FakeToolContext()))["error"] == "unauthenticated"
    assert run(t.submit("P", FakeToolContext()))["error"] == "unauthenticated"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("submit_work_order_proposal", {"proposal_id": "P", "approved": True}),
        ("submit_work_order_proposal", {"proposal_id": "P", "confirmed": True}),
        ("prepare_work_order_proposal", {**ARGS, "unit_id": "UNIT-004"}),
        ("prepare_work_order_proposal", {**ARGS, "resident_id": "RES-004"}),
        ("prepare_work_order_proposal", {**ARGS, "property_id": "PROP-002"}),
    ],
)
def test_guard_blocks_approval_and_identity_arguments(tool, args):
    result = make_before_tool_callback(10)(SimpleNamespace(name=tool), args, FakeToolContext())
    assert result["error"] == "invalid_arguments"


def _declared(agent):
    out = {}
    for t in agent.tools:
        d = t._get_declaration()
        schema = d.parameters_json_schema or (d.parameters.model_dump() if d.parameters else {})
        out[d.name] = set(schema.get("properties", {}))
    return out


def test_creation_tools_registered_only_when_enabled(valid_env):
    s = get_settings()
    ro = build_agent(s, StaticIdentityProvider(None), lambda: None, lambda: None)
    assert set(_declared(ro)) == {"search_policies", "list_my_work_orders"}

    rw = build_agent(s, StaticIdentityProvider(None), lambda: None, lambda: None, lambda: None)
    declared = _declared(rw)
    assert declared["submit_work_order_proposal"] == {"proposal_id"}
    for params in declared.values():
        assert params.isdisjoint({"approved", "confirmed", "unit_id", "resident_id", "property_id"})


def test_prompts_cover_stage2_rules():
    assert "not available in this" in INSTRUCTION
    for rule in (
        "entry preference",
        "chat message alone is not approval",
        'returns\n     status "executed"',
        "outcome_unknown",
        "safety and escalation guidance first",
        "Same category alone does not mean same",
    ):
        assert rule in INSTRUCTION_WITH_CREATION, rule


def test_list_my_proposals_is_owner_scoped_and_shows_lifecycle():
    svc, _, clock = make_service(ttl=600)
    owner = make_creation_tools("AUTH-RES-002", service=svc)
    pid = prepared(owner)
    listed = run(owner.mine(FakeToolContext()))
    assert [p["proposal_id"] for p in listed["proposals"]] == [pid]
    assert listed["proposals"][0]["status"] == "awaiting_approval"
    assert "resident_id" not in json.dumps(listed)

    other = make_creation_tools("AUTH-RES-001", service=svc)
    assert run(other.mine(FakeToolContext()))["proposals"] == []

    clock.advance(seconds=601)
    assert run(owner.mine(FakeToolContext()))["proposals"][0]["status"] == "expired"


def test_list_my_proposals_reports_executed_work_order():
    t = make_creation_tools()
    pid = prepared(t)
    ctx, _ = request(t, pid)
    run(t.submit(pid, FakeToolContext(confirmed(True), ctx.function_call_id)))
    [p] = run(t.mine(FakeToolContext()))["proposals"]
    assert (p["status"], p["work_order_id"]) == ("executed", "WO-011")


def test_superseded_proposals_are_not_listed():
    t = make_creation_tools()
    prepared(t)
    newer = prepared(t, priority="urgent")
    assert [p["proposal_id"] for p in run(t.mine(FakeToolContext()))["proposals"]] == [newer]
