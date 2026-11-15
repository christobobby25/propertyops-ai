"""Firestore target selection must fail closed and never fall back to cloud."""

import socket
from types import SimpleNamespace

import pytest

from app import emulator
from app.config import ConfigError, get_settings
from app.gcp import FirestoreTargetError, check_emulator_reachable, firestore_client


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_emulator_mode_requires_host(valid_env):
    valid_env.setenv("FIRESTORE_TARGET", "emulator")
    valid_env.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    with pytest.raises(FirestoreTargetError, match="not set"):
        firestore_client(get_settings())


def test_emulator_mode_requires_reachable_emulator(valid_env):
    valid_env.setenv("FIRESTORE_TARGET", "emulator")
    valid_env.setenv("FIRESTORE_EMULATOR_HOST", f"127.0.0.1:{_free_port()}")
    with pytest.raises(FirestoreTargetError, match="unreachable"):
        firestore_client(get_settings())


@pytest.mark.parametrize("host", ["firestore.googleapis.com:443", "10.0.0.5:8085", "nope"])
def test_emulator_host_must_be_local(host):
    with pytest.raises(FirestoreTargetError):
        check_emulator_reachable(host)


def test_cloud_mode_refuses_when_emulator_host_set(valid_env):
    valid_env.setenv("FIRESTORE_TARGET", "cloud")
    valid_env.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8085")
    with pytest.raises(FirestoreTargetError, match="ambiguous"):
        firestore_client(get_settings())


def test_emulator_project_must_be_demo(valid_env):
    valid_env.setenv("EMULATOR_PROJECT_ID", "project-3502762d-c2c8-4d67-a2e")
    with pytest.raises(ConfigError, match="EMULATOR_PROJECT_ID"):
        get_settings()


def test_reset_refuses_non_demo_project(monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8085")
    with pytest.raises(FirestoreTargetError, match="non-demo"):
        emulator.clear(SimpleNamespace(project="real-project"))


def test_reset_refuses_without_emulator_host(monkeypatch):
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    with pytest.raises(FirestoreTargetError):
        emulator.clear(SimpleNamespace(project="demo-propertyops"))


def test_creation_disabled_by_default(valid_env):
    s = get_settings()
    assert s.enable_work_order_creation is False and s.firestore_target == "cloud"
