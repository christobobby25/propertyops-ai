import pytest

from app.auth import AuthContext, AuthorizationError, PropertyScopeRequired, authorize_property
from app.demo_auth import load_demo_context


def resident(props=("PROP-001",)):
    return AuthContext(
        subject="r", role="resident", resident_id="RES-1", allowed_property_ids=frozenset(props)
    )


def test_single_property_is_implied():
    assert authorize_property(resident()) == "PROP-001"


def test_requested_property_within_scope():
    assert authorize_property(resident(), "PROP-001") == "PROP-001"


def test_requested_property_outside_scope_rejected():
    with pytest.raises(AuthorizationError, match="PROP-002"):
        authorize_property(resident(), "PROP-002")


def test_multi_property_actor_must_choose():
    ctx = resident(("PROP-001", "PROP-002"))
    with pytest.raises(PropertyScopeRequired):
        authorize_property(ctx)
    assert authorize_property(ctx, "PROP-002") == "PROP-002"


def test_no_access_rejected():
    with pytest.raises(AuthorizationError):
        authorize_property(resident(()))


def test_context_validation():
    with pytest.raises(ValueError, match="role"):
        AuthContext(subject="x", role="admin", allowed_property_ids=frozenset())
    with pytest.raises(ValueError, match="resident_id"):
        AuthContext(subject="x", role="resident", allowed_property_ids=frozenset())


def test_scope_is_immutable():
    ctx = AuthContext(subject="m", role="property_manager", allowed_property_ids={"PROP-001"})
    assert isinstance(ctx.allowed_property_ids, frozenset)
    with pytest.raises(AttributeError):
        ctx.allowed_property_ids = frozenset({"PROP-002"})


def test_demo_fixtures_load():
    res = load_demo_context("AUTH-RES-004")
    assert res.role == "resident" and res.allowed_property_ids == {"PROP-002"}
    mgr = load_demo_context("AUTH-MGR-001")
    assert mgr.role == "property_manager" and mgr.resident_id is None
    with pytest.raises(KeyError):
        load_demo_context("AUTH-NOPE")
