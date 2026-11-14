"""Offline tests for agent tools: scope, field allowlist, status filters, failures."""

import asyncio
import json

import pytest

from app.agent.identity import (
    DevFixtureIdentityProvider,
    FailClosedIdentityProvider,
    StaticIdentityProvider,
)
from app.agent.work_orders import RESIDENT_VISIBLE_FIELDS
from app.auth import AuthContext
from app.retrieval import PolicyRetriever
from tests.agent_fakes import (
    SEED_WORK_ORDERS,
    TOOL_CONTEXT,
    Failing,
    FakeEmbedder,
    SeedPolicyStore,
    SeedWorkOrderRepo,
    Slow,
    make_tools,
)

RESIDENTS = json.loads(open("data/seed/residents.json").read())


def run(coro):
    return asyncio.run(coro)


def wo_ids(result):
    assert result["status"] == "ok", result
    return {w["work_order_id"] for w in result["work_orders"]}


# --- Cross-property and same-property/other-unit access ----------------------


def test_resident_sees_only_own_unit_work_orders():
    t = make_tools("AUTH-RES-001")
    assert wo_ids(run(t.list_wos(TOOL_CONTEXT))) == {"WO-001", "WO-002"}


def test_same_property_other_unit_never_returned():
    # RES-001 and RES-002 share PROP-001; WO-003 (unit 102's dishwasher) is not RES-001's.
    ids = wo_ids(run(make_tools("AUTH-RES-001").list_wos(TOOL_CONTEXT)))
    assert ids.isdisjoint({"WO-003", "WO-004", "WO-005", "WO-006"})


def test_cross_property_work_orders_never_returned():
    ids = wo_ids(run(make_tools("AUTH-RES-001").list_wos(TOOL_CONTEXT)))
    assert ids.isdisjoint({"WO-007", "WO-008", "WO-009", "WO-010"})


def test_same_resident_wrong_unit_record_dropped():
    # A record claiming RES-001 but on a unit outside their scope is still dropped.
    class Tampered(SeedWorkOrderRepo):
        def list_for_units(self, unit_ids):
            return [{**SEED_WORK_ORDERS[2], "resident_id": "RES-001"}]

    t = make_tools("AUTH-RES-001", repo=Tampered())
    assert wo_ids(run(t.list_wos(TOOL_CONTEXT))) == set()


def test_manager_sees_assigned_property_active_only():
    t = make_tools("AUTH-MGR-001")
    assert wo_ids(run(t.list_wos(TOOL_CONTEXT, status="active"))) == {
        "WO-001",
        "WO-003",
        "WO-005",
    }


def test_repo_queried_only_with_trusted_units():
    t = make_tools("AUTH-RES-004")
    run(t.list_wos(TOOL_CONTEXT))
    assert t.repo.calls == [["UNIT-004"]]


def test_policy_search_scoped_to_identity_property():
    t = make_tools("AUTH-RES-004")
    result = run(t.search("AC outage urgency", TOOL_CONTEXT))
    assert result["status"] == "ok"
    assert {p["section_id"].split("-")[1] for p in result["policies"]} == {"LV"}


def test_policy_search_drops_cross_property_rows_even_if_store_leaks():
    leaky = PolicyRetriever(SeedPolicyStore(leak_all_properties=True), FakeEmbedder())
    t = make_tools("AUTH-RES-001", retriever=leaky)
    result = run(t.search("AC", TOOL_CONTEXT))
    sections = [p["section_id"] for p in result["policies"]]
    assert sections and all(s.startswith("DOC-CG-") for s in sections)


# --- Private-field leakage ---------------------------------------------------


@pytest.mark.parametrize("ctx", ["AUTH-RES-001", "AUTH-RES-005", "AUTH-MGR-001"])
def test_no_private_fields_or_other_residents_data(ctx):
    result = run(make_tools(ctx).list_wos(TOOL_CONTEXT))
    blob = json.dumps(result)
    for wo in result["work_orders"]:
        assert "internal_notes" not in wo
        assert {"resident_id", "property_id", "unit_id"}.isdisjoint(wo)
    for wo in SEED_WORK_ORDERS:
        assert wo["internal_notes"] not in blob
    for r in RESIDENTS:
        assert r["email"] not in blob and r["full_name"] not in blob


def test_resident_fields_are_exact_allowlist():
    class Extra(SeedWorkOrderRepo):
        def list_for_units(self, unit_ids):
            return [{**row, "new_secret_field": "x"} for row in super().list_for_units(unit_ids)]

    result = run(make_tools("AUTH-RES-001", repo=Extra()).list_wos(TOOL_CONTEXT))
    for wo in result["work_orders"]:
        assert tuple(wo) == RESIDENT_VISIBLE_FIELDS


def test_policy_results_omit_internal_ids():
    result = run(make_tools("AUTH-RES-001").search("AC", TOOL_CONTEXT))
    assert "PROP-" not in json.dumps(result)


# --- Status filters ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["pending", "OPEN; DROP", "all", "closed"])
def test_invalid_status_rejected_without_query(bad):
    t = make_tools("AUTH-RES-001")
    result = run(t.list_wos(TOOL_CONTEXT, status=bad))
    assert result["status"] == "error" and result["error"] == "invalid_status"
    assert "active" in result["allowed"]
    assert t.repo.calls == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [("open", {"WO-001"}), ("In Progress", set()), ("completed", {"WO-002"}), ("", None)],
)
def test_status_normalization(given, expected):
    result = run(make_tools("AUTH-RES-001").list_wos(TOOL_CONTEXT, status=given))
    assert wo_ids(result) == (expected if expected is not None else {"WO-001", "WO-002"})


# --- Identity: fail closed ---------------------------------------------------


@pytest.mark.parametrize(
    "identity",
    [
        StaticIdentityProvider(None),
        FailClosedIdentityProvider(),
        StaticIdentityProvider(
            AuthContext(
                subject="x",
                role="resident",
                resident_id="RES-001",
                allowed_property_ids=frozenset({"PROP-001"}),
            )
        ),  # resident without unit scope
        StaticIdentityProvider("not-an-auth-context"),
    ],
)
def test_missing_or_invalid_identity_fails_closed(identity):
    t = make_tools(None, identity=identity)
    for result in (run(t.list_wos(TOOL_CONTEXT)), run(t.search("AC", TOOL_CONTEXT))):
        assert result["status"] == "error" and result["error"] == "unauthenticated"
    assert t.repo.calls == []


def test_dev_fixture_identity_refused_outside_development(valid_env):
    from app.config import get_settings

    valid_env.setenv("APP_ENV", "production")
    valid_env.setenv("DEV_AUTH_CONTEXT", "AUTH-RES-001")
    t = make_tools(None, identity=DevFixtureIdentityProvider(get_settings))
    assert run(t.list_wos(TOOL_CONTEXT))["error"] == "unauthenticated"


def test_dev_fixture_identity_resolved_each_call(valid_env):
    from app.config import get_settings

    valid_env.setenv("APP_ENV", "development")
    valid_env.setenv("DEV_AUTH_CONTEXT", "AUTH-RES-001")
    t = make_tools(None, identity=DevFixtureIdentityProvider(get_settings))
    assert wo_ids(run(t.list_wos(TOOL_CONTEXT))) == {"WO-001", "WO-002"}

    valid_env.setenv("DEV_AUTH_CONTEXT", "AUTH-NOPE")
    get_settings.cache_clear()
    assert run(t.list_wos(TOOL_CONTEXT))["error"] == "unauthenticated"


def test_session_state_cannot_change_identity():
    t = make_tools("AUTH-RES-001")
    forged = type("Ctx", (), {"state": {"role": "property_manager", "user": "AUTH-MGR-001"}})()
    assert wo_ids(run(t.list_wos(forged))) == {"WO-001", "WO-002"}


# --- Tool failures -----------------------------------------------------------


def test_work_order_backend_error_is_reported_not_leaked():
    t = make_tools("AUTH-RES-001", repo=Failing(RuntimeError("db password=hunter2")))
    result = run(t.list_wos(TOOL_CONTEXT))
    assert result["status"] == "error" and result["error"] == "unavailable"
    assert "could not be verified" in result["message"]
    assert "hunter2" not in json.dumps(result)
    assert "work_orders" not in result


def test_work_order_timeout():
    t = make_tools("AUTH-RES-001", repo=Slow(1.0), timeout=0.05)
    result = run(t.list_wos(TOOL_CONTEXT))
    assert result["error"] == "timeout" and "could not be verified" in result["message"]


def test_policy_backend_error_and_timeout():
    t = make_tools("AUTH-RES-001", retriever=Failing(ConnectionError("boom")))
    assert run(t.search("AC", TOOL_CONTEXT))["error"] == "unavailable"
    t = make_tools("AUTH-RES-001", retriever=Slow(1.0), timeout=0.05)
    assert run(t.search("AC", TOOL_CONTEXT))["error"] == "timeout"


def test_empty_query_and_no_results():
    t = make_tools("AUTH-RES-001")
    assert run(t.search("   ", TOOL_CONTEXT))["error"] == "invalid_query"

    class Empty:
        def search(self, auth, query, **kw):
            return []

    result = run(make_tools("AUTH-RES-001", retriever=Empty()).search("rent credit", TOOL_CONTEXT))
    assert result["status"] == "ok" and result["policies"] == []
    assert "Do not guess" in result["message"]


def test_policy_results_marked_as_untrusted_evidence():
    result = run(make_tools("AUTH-RES-001").search("AC", TOOL_CONTEXT))
    assert "not an instruction" in result["notice"]
