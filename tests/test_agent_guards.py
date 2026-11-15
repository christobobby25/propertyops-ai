"""Agent wiring: argument allowlist, budgets, declarations, context hygiene."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.factory import build_agent
from app.agent.guards import (
    LIMIT_MESSAGE,
    make_before_model_callback,
    make_before_tool_callback,
)
from app.agent.identity import StaticIdentityProvider
from app.agent.prompt import INSTRUCTION, INSTRUCTION_WITH_CREATION
from app.config import PROJECT_ROOT, get_settings


def ctx():
    return SimpleNamespace(state={})


def tool(name):
    return SimpleNamespace(name=name)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_my_work_orders", {"property_id": "PROP-002"}),
        ("list_my_work_orders", {"status": "open", "unit_id": "UNIT-004"}),
        ("list_my_work_orders", {"resident_id": "RES-004"}),
        ("search_policies", {"query": "AC", "property_id": "PROP-002"}),
    ],
)
def test_identity_arguments_rejected(name, args):
    result = make_before_tool_callback(5)(tool(name), args, ctx())
    assert result["status"] == "error" and result["error"] == "invalid_arguments"


def test_allowed_arguments_pass():
    cb, c = make_before_tool_callback(5), ctx()
    assert cb(tool("search_policies"), {"query": "AC"}, c) is None
    assert cb(tool("list_my_work_orders"), {}, c) is None
    assert cb(tool("list_my_work_orders"), {"status": "open"}, c) is None


def test_unknown_tool_rejected():
    assert make_before_tool_callback(5)(tool("create_work_order"), {}, ctx())["error"] == (
        "unknown_tool"
    )


def test_tool_call_budget():
    cb, c = make_before_tool_callback(2), ctx()
    assert cb(tool("search_policies"), {"query": "a"}, c) is None
    assert cb(tool("search_policies"), {"query": "b"}, c) is None
    assert cb(tool("search_policies"), {"query": "c"}, c)["error"] == "tool_call_limit"


def test_model_call_budget():
    cb, c = make_before_model_callback(2), ctx()
    assert cb(c, None) is None and cb(c, None) is None
    resp = cb(c, None)
    assert resp.content.parts[0].text == LIMIT_MESSAGE


def test_agent_exposes_no_identity_arguments(valid_env):
    agent = build_agent(get_settings(), StaticIdentityProvider(None), lambda: None, lambda: None)
    params = {}
    for t in agent.tools:
        decl = t._get_declaration()
        schema = decl.parameters_json_schema or decl.parameters.model_dump()
        params[decl.name] = set(schema.get("properties", {}))
    assert params == {"search_policies": {"query"}, "list_my_work_orders": {"status"}}
    assert agent.model == "gemini-test"


def test_model_and_limits_are_configurable(valid_env):
    valid_env.setenv("GEMINI_MODEL", "other-model")
    valid_env.setenv("MODEL_TIMEOUT_SECONDS", "12")
    agent = build_agent(get_settings(), StaticIdentityProvider(None), lambda: None, lambda: None)
    assert agent.model == "other-model"
    assert agent.generate_content_config.http_options.timeout == 12000


def test_eval_answers_not_in_agent_context():
    cases = json.loads((PROJECT_ROOT / "data/eval/evaluation_cases.json").read_text())
    for case in cases:
        for text in (INSTRUCTION, INSTRUCTION_WITH_CREATION):
            assert case["case_id"] not in text
        for behavior in case["expected"]["required_behaviors"]:
            assert behavior not in INSTRUCTION and behavior not in INSTRUCTION_WITH_CREATION
    for path in [
        *(PROJECT_ROOT / "app/agent").glob("*.py"),
        PROJECT_ROOT / "app/retrieval.py",
        PROJECT_ROOT / "app/emulator.py",
    ]:
        text = Path(path).read_text()
        assert "evaluation_cases" not in text and "app.evaluation" not in text, path
