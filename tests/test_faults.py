import pytest

from app import faults
from app.web.server import _fault_config


def test_parse_and_validation():
    assert faults.parse("work_orders_timeout=0.3, retrieval_error=1") == {
        "work_orders_timeout": 0.3,
        "retrieval_error": 1.0,
    }
    for bad in ("nope=0.1", "retrieval_error=2", "retrieval_error=x"):
        with pytest.raises(faults.FaultConfigError):
            faults.parse(bad)


def test_faults_refused_outside_development(valid_env, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("FAULT_INJECTION", "retrieval_error=0.5")
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        _fault_config(get_settings())


def test_faulty_wrappers():
    class Inner:
        def list_for_units(self, u):
            return ["ok"]

        def search(self, a, q, **kw):
            return ["ok"]

    always = lambda: 0.0  # noqa: E731
    never = lambda: 1.0  # noqa: E731
    with pytest.raises(ConnectionError):
        faults.FaultyWorkOrders(
            Inner(), {"work_orders_error": 0.5}, 0.0, rng=always
        ).list_for_units([])
    assert faults.FaultyRetriever(Inner(), {"retrieval_error": 0.5}, 0.0, rng=never).search(
        None, "q"
    ) == ["ok"]
