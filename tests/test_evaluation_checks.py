"""Offline tests for the live evaluator's deterministic checks and harness."""

import asyncio

from app.demo_auth import load_demo_context
from app.evaluation.cases import load_cases
from app.evaluation.checks import check_citations, check_disclosure, check_tool_use
from app.evaluation.runner import CaseRun
from app.seed_data import load_seed_collection
from scripts import run_live_eval

CASES = {c.case_id: c for c in load_cases()}
NOTE = load_seed_collection("work_orders")[0]["internal_notes"]


def run_with(answer="", calls=(), responses=()):
    return CaseRun(final_text=answer, tool_calls=list(calls), tool_responses=list(responses))


def search_resp(*sections):
    return {
        "name": "search_policies",
        "response": {"status": "ok", "policies": [{"section_id": s} for s in sections]},
    }


def test_stage2_cases_skipped_and_stage1_supported():
    skipped = {cid for cid, c in CASES.items() if c.skip_reason}
    assert skipped == {"EVAL-006", "EVAL-014", "EVAL-015", "EVAL-020"}
    assert CASES["EVAL-013"].skip_reason is None  # tool fault is simulated
    assert CASES["EVAL-019"].skip_reason is None  # retrieval injection is simulated


def test_tool_use_requires_expected_tools():
    case = CASES["EVAL-001"]
    assert not check_tool_use(case, run_with(calls=[{"name": "search_policies"}])).passed
    both = [{"name": "search_policies"}, {"name": "list_my_work_orders"}]
    assert check_tool_use(case, run_with(calls=both)).passed


def test_tool_fault_alias_maps_to_agent_tool():
    case = CASES["EVAL-013"]
    assert check_tool_use(case, run_with(calls=[{"name": "list_my_work_orders"}])).passed


def test_citation_must_be_retrieved_and_relevant():
    case = CASES["EVAL-002"]
    ok = run_with("Routine [DOC-CG-HVAC-S01].", responses=[search_resp("DOC-CG-HVAC-S01")])
    assert check_citations(case, ok).passed
    fabricated = run_with("Routine [DOC-CG-HVAC-S02].", responses=[search_resp("DOC-CG-HVAC-S01")])
    assert not check_citations(case, fabricated).passed
    uncited = run_with("Routine.", responses=[search_resp("DOC-CG-HVAC-S01")])
    assert not check_citations(case, uncited).passed


def test_disclosure_flags_out_of_scope_records():
    auth = load_demo_context("AUTH-RES-001")
    assert check_disclosure(auth, run_with("Your WO-001 is open.")).passed
    for leak in ("WO-003 is scheduled", "See DOC-LV-HVAC", "unit UNIT-002", NOTE):
        assert not check_disclosure(auth, run_with(leak)).passed, leak


def test_manager_scope_in_disclosure():
    auth = load_demo_context("AUTH-MGR-001")
    assert check_disclosure(auth, run_with("WO-003 and WO-005")).passed
    assert not check_disclosure(auth, run_with("WO-007")).passed


def test_rate_limit_retry(monkeypatch):
    monkeypatch.setattr(run_live_eval, "RATE_LIMIT_BACKOFF_SECONDS", (0, 0))
    attempts = []

    async def flaky():
        attempts.append(1)
        return CaseRun(error="429 RESOURCE_EXHAUSTED" if len(attempts) < 2 else None)

    result, retries = asyncio.run(run_live_eval._with_rate_limit_retry(flaky))
    assert result.error is None and retries == 1
