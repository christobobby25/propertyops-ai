"""Web app tests: auth modes, scoping, the approve/cancel boundary, headers.

The agent run itself is replaced by a stub runner so these tests are offline;
the confirmation round trip is exercised through ChatService."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent.identity import DemoDirectory, SubjectDirectoryIdentityProvider
from app.config import get_settings
from app.web.auth import AuthenticationError, Authenticator, StartupConfigError
from app.web.chat import ChatService
from app.web.server import create_app
from tests.agent_fakes import SEED_WORK_ORDERS, SeedWorkOrderRepo


def fc(name, args=None, id_="fc-1"):
    return SimpleNamespace(name=name, args=args or {}, id=id_)


class Event:
    def __init__(self, calls=(), responses=(), text=None):
        self._calls, self._responses, self.text = list(calls), list(responses), text
        parts = [SimpleNamespace(text=text)] if text else []
        self.content = SimpleNamespace(parts=parts) if parts else None

    def get_function_calls(self):
        return self._calls

    def get_function_responses(self):
        return self._responses

    def is_final_response(self):
        return self.text is not None


class StubRunner:
    """Scripted ADK runner: a confirmation request, then an executed result."""

    def __init__(self):
        self.calls = []

    async def run_async(self, *, user_id, session_id, new_message, run_config):
        self.calls.append((user_id, session_id, new_message))
        part = new_message.parts[0]
        if part.function_response is not None:
            confirmed = part.function_response.response["confirmed"]
            resp = SimpleNamespace(
                name="submit_work_order_proposal",
                response={"status": "executed", "work_order_id": "WO-011", "newly_created": True}
                if confirmed
                else {"status": "declined"},
            )
            yield Event(responses=[resp], text="Created WO-011." if confirmed else "Not created.")
            return
        hint = "Create this maintenance request? (proposal PROPOSAL-1)\nPriority: routine"
        yield Event(calls=[fc("submit_work_order_proposal")])
        yield Event(
            calls=[
                fc(
                    "adk_request_confirmation",
                    {"toolConfirmation": {"hint": hint, "payload": {"proposal_id": "PROPOSAL-1"}}},
                    id_=f"conf-{len(self.calls)}",
                )
            ],
            text="Please confirm in the dialog.",
        )


@pytest.fixture
def demo_env(valid_env):
    valid_env.setenv("APP_ENV", "development")
    valid_env.setenv("AUTH_MODE", "demo")
    valid_env.setenv("DEV_AUTH_CONTEXT", "AUTH-RES-002")
    return valid_env


def make_client(runner=None):
    s = get_settings()
    runner = runner or StubRunner()
    chat = ChatService(s, agent=None, runner=runner)
    app = create_app(s, chat=chat, directory=DemoDirectory(), work_orders=SeedWorkOrderRepo())
    return TestClient(app), runner


SID = "s0123456789abcdef"


def as_(ctx):
    return {"X-Demo-Identity": ctx}


# --- Auth configuration --------------------------------------------------------------


def test_demo_auth_refused_outside_development(valid_env):
    valid_env.setenv("APP_ENV", "production")
    valid_env.setenv("AUTH_MODE", "demo")
    with pytest.raises(StartupConfigError):
        Authenticator(get_settings())


def test_google_mode_requires_audience(valid_env):
    valid_env.setenv("AUTH_MODE", "google_id_token")
    with pytest.raises(StartupConfigError):
        Authenticator(get_settings())


def test_google_token_verified_and_prefixed(valid_env):
    valid_env.setenv("AUTH_MODE", "google_id_token")
    valid_env.setenv("OAUTH_AUDIENCE", "client-123")
    seen = {}

    def verify(token, audience):
        seen["args"] = (token, audience)
        if token != "good":
            raise ValueError("bad")
        return {"sub": "1234", "email_verified": True}

    auth = Authenticator(get_settings(), verify_token=verify)
    assert auth.authenticate({"Authorization": "Bearer good"}).subject == "google:1234"
    assert seen["args"] == ("good", "client-123")
    for headers in ({}, {"Authorization": "Bearer bad"}, {"Authorization": "Basic x"}):
        with pytest.raises(AuthenticationError):
            auth.authenticate(headers)
    # The demo header means nothing in this mode.
    with pytest.raises(AuthenticationError):
        auth.authenticate({"X-Demo-Identity": "AUTH-MGR-001"})


# --- Scoping -----------------------------------------------------------------------------


def test_work_orders_scoped_to_authenticated_user(demo_env):
    client, _ = make_client()
    r1 = client.get("/api/work-orders", headers=as_("AUTH-RES-001")).json()
    assert {w["work_order_id"] for w in r1["work_orders"]} == {"WO-001", "WO-002"}
    r4 = client.get("/api/work-orders", headers=as_("AUTH-RES-004")).json()
    assert {w["work_order_id"] for w in r4["work_orders"]} == {"WO-007", "WO-008"}
    blob = json.dumps(r1)
    assert all(w["internal_notes"] not in blob for w in SEED_WORK_ORDERS)


def test_unknown_identity_and_bad_status(demo_env):
    client, _ = make_client()
    assert client.get("/api/me", headers=as_("AUTH-NOPE")).status_code == 401
    r = client.get("/api/work-orders?status=bogus", headers=as_("AUTH-RES-001"))
    assert r.status_code == 400


def test_runner_user_id_is_the_authenticated_subject(demo_env):
    client, runner = make_client()
    client.post("/api/chat", headers=as_("AUTH-RES-004"), json={"session_id": SID, "message": "hi"})
    assert runner.calls[0][0] == "demo-resident-004"


# --- Approval boundary ---------------------------------------------------------------------


def test_approve_flow_only_accepts_pending_confirmation(demo_env):
    client, runner = make_client()
    r = client.post(
        "/api/chat", headers=as_("AUTH-RES-002"), json={"session_id": SID, "message": "go"}
    )
    pending = r.json()["pending_confirmation"]
    assert pending["proposal_id"] == "PROPOSAL-1" and "Priority: routine" in pending["display"]

    # Wrong confirmation id, or another user replaying it, is rejected.
    bad = {"session_id": SID, "confirmation_id": "conf-x", "approve": True}
    assert client.post("/api/confirm", headers=as_("AUTH-RES-002"), json=bad).status_code == 409
    replay = {"session_id": SID, "confirmation_id": pending["confirmation_id"], "approve": True}
    assert client.post("/api/confirm", headers=as_("AUTH-RES-001"), json=replay).status_code == 409

    ok = client.post("/api/confirm", headers=as_("AUTH-RES-002"), json=replay).json()
    assert ok["created_work_order"]["work_order_id"] == "WO-011"
    # A confirmation can be used once.
    assert client.post("/api/confirm", headers=as_("AUTH-RES-002"), json=replay).status_code == 409


def test_cancel_sends_declined(demo_env):
    client, runner = make_client()
    r = client.post(
        "/api/chat", headers=as_("AUTH-RES-002"), json={"session_id": SID, "message": "go"}
    )
    cid = r.json()["pending_confirmation"]["confirmation_id"]
    body = {"session_id": SID, "confirmation_id": cid, "approve": False}
    out = client.post("/api/confirm", headers=as_("AUTH-RES-002"), json=body).json()
    assert out["created_work_order"] is None
    assert runner.calls[-1][2].parts[0].function_response.response == {"confirmed": False}


def test_new_message_abandons_pending_dialog(demo_env):
    client, _ = make_client()
    r = client.post(
        "/api/chat", headers=as_("AUTH-RES-002"), json={"session_id": SID, "message": "go"}
    )
    cid = r.json()["pending_confirmation"]["confirmation_id"]
    client.post(
        "/api/chat", headers=as_("AUTH-RES-002"), json={"session_id": SID, "message": "wait"}
    )
    body = {"session_id": SID, "confirmation_id": cid, "approve": True}
    assert client.post("/api/confirm", headers=as_("AUTH-RES-002"), json=body).status_code == 409


def test_input_validation(demo_env):
    client, _ = make_client()
    h = as_("AUTH-RES-002")
    assert client.post(
        "/api/chat", headers=h, json={"session_id": "bad id!", "message": "x"}
    ).status_code in (400, 422)
    assert (
        client.post(
            "/api/chat", headers=h, json={"session_id": SID, "message": "x" * 2001}
        ).status_code
        == 422
    )


# --- Identity provider used by the web agent --------------------------------------------------


def test_subject_directory_provider_resolves_each_call():
    provider = SubjectDirectoryIdentityProvider(DemoDirectory())
    assert provider.resolve(SimpleNamespace(user_id="demo-resident-004")).resident_id == "RES-004"
    from app.agent.identity import IdentityError

    for ctx in (SimpleNamespace(user_id=None), SimpleNamespace(user_id="google:unknown")):
        with pytest.raises(IdentityError):
            provider.resolve(ctx)


# --- Static UI and headers ----------------------------------------------------------------------


def test_index_and_security_headers(demo_env):
    client, _ = make_client()
    r = client.get("/")
    assert r.status_code == 200 and "PropertyOps Assistant" in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in r.text  # no inline scripts
    assert client.get("/static/app.js").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["auth_mode"] == "demo" and len(cfg["demo_identities"]) == 6


def test_json_logs_hash_subjects():
    import logging

    from app.logging_config import JsonFormatter

    rec = logging.makeLogRecord(
        {
            "msg": "turn_complete",
            "levelname": "INFO",
            "subject": "demo-resident-002",
            "latency_ms": 12,
        }
    )
    out = json.loads(JsonFormatter().format(rec))
    assert out["severity"] == "INFO" and out["latency_ms"] == 12
    assert "subject" not in out and len(out["subject_hash"]) == 16
    assert "demo-resident-002" not in json.dumps(out)


def test_request_id_and_trace_correlation(demo_env, caplog):
    import logging

    client, _ = make_client()
    with caplog.at_level(logging.INFO, logger="propertyops.web"):
        r = client.get(
            "/api/work-orders",
            headers={**as_("AUTH-RES-001"), "X-Cloud-Trace-Context": "abc123/1;o=1"},
        )
    assert len(r.headers["X-Request-ID"]) == 16
    [rec] = [x for x in caplog.records if getattr(x, "event", None) == "http_request"]
    assert rec.status == 200 and rec.path == "/api/work-orders"


def test_tool_telemetry_flags_authz_denials(caplog):
    import logging

    from app import observability

    ctx = SimpleNamespace(state={}, function_call_id="c1", user_id="demo-resident-001")
    tool = SimpleNamespace(name="submit_work_order_proposal")
    observability.before_tool(tool, {}, ctx)
    with caplog.at_level(logging.INFO, logger="propertyops.telemetry"):
        observability.after_tool(tool, {}, ctx, {"status": "error", "error": "not_found"})
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events == ["tool_call", "authz_denied"]
    assert caplog.records[0].latency_ms is not None


def test_trace_header_parsing():
    from app.observability import trace_from_header

    assert trace_from_header("abc/1;o=1", "p") == "projects/p/traces/abc"
    assert trace_from_header(None, "p") is None


def test_provision_scope_derived_from_seed():
    from scripts.provision_identity import scope_for

    assert scope_for("resident", "RES-002", []) == {
        "role": "resident",
        "resident_id": "RES-002",
        "allowed_property_ids": ["PROP-001"],
        "allowed_unit_ids": ["UNIT-002"],
    }
    mgr = scope_for("property_manager", None, ["PROP-002"])
    assert mgr["allowed_unit_ids"] == ["UNIT-004", "UNIT-005"]
    for bad in (("resident", "RES-999", []), ("property_manager", None, ["PROP-999"])):
        with pytest.raises(ValueError):
            scope_for(*bad)


def test_firestore_directory_respects_active_flag():
    from app.agent.identity import FirestoreIdentityDirectory

    docs = {
        "google:1": {
            "active": True,
            "role": "resident",
            "resident_id": "RES-001",
            "allowed_property_ids": ["PROP-001"],
            "allowed_unit_ids": ["UNIT-001"],
        },
        "google:2": {
            "active": False,
            "role": "resident",
            "resident_id": "RES-002",
            "allowed_property_ids": ["PROP-001"],
            "allowed_unit_ids": ["UNIT-002"],
        },
    }

    class Snap:
        def __init__(self, d):
            self.d = d
            self.exists = d is not None

        def get(self, k):
            return self.d.get(k)

        def to_dict(self):
            return dict(self.d)

    class DB:
        def collection(self, _):
            return self

        def document(self, k):
            return SimpleNamespace(get=lambda: Snap(docs.get(k)))

    d = FirestoreIdentityDirectory(DB())
    assert d.lookup("google:1").resident_id == "RES-001"
    assert d.lookup("google:2") is None and d.lookup("google:3") is None


def test_deploy_commands_are_least_privilege_and_production(valid_env, capsys):
    from scripts import print_deploy_commands

    assert print_deploy_commands.main(["--oauth-client-id", "abc.apps.googleusercontent.com"]) == 0
    out = capsys.readouterr().out
    assert "AUTH_MODE=google_id_token" in out and "APP_ENV=production" in out
    assert "ENABLE_WORK_ORDER_CREATION=false" in out and "--max-instances=1" in out
    assert "roles/owner" not in out and "roles/editor" not in out


def test_google_mode_csp_allows_only_gsi(valid_env):
    valid_env.setenv("AUTH_MODE", "google_id_token")
    valid_env.setenv("OAUTH_AUDIENCE", "client-123")
    s = get_settings()
    auth = Authenticator(s, verify_token=lambda t, a: {"sub": "42", "email_verified": True})
    app = create_app(
        s,
        chat=ChatService(s, None, runner=StubRunner()),
        directory=DemoDirectory(),
        work_orders=SeedWorkOrderRepo(),
        authenticator=auth,
    )
    client = TestClient(app)
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self' https://accounts.google.com/gsi/" in csp
    assert "frame-ancestors 'none'" in csp
    assert client.get("/health").json() == {"ok": True}
    # Verified but unprovisioned: 403 tells the caller their own account ID.
    r = client.get("/api/me", headers={"Authorization": "Bearer t"})
    assert r.status_code == 403 and r.json()["detail"]["subject"] == "google:42"
    assert "demo_identities" not in client.get("/api/config").json()
