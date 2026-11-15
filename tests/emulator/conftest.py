"""Emulator-backed fixtures. Tests here are marked `emulator`.

Without FIRESTORE_EMULATOR_HOST they are skipped locally; with
REQUIRE_EMULATOR=1 (CI emulator job) a missing/unreachable emulator FAILS.
"""

import os
from datetime import datetime

import pytest

from app import emulator
from app.agent.clock import FixedClock
from app.agent.proposals import FirestoreProposalRepository, ProposalService
from app.agent.work_orders import FirestoreWorkOrderRepository
from app.config import get_settings
from app.gcp import FirestoreTargetError, firestore_client
from app.retrieval import FirestorePolicyStore
from tests.conftest import VALID_ENV

pytestmark = pytest.mark.emulator
SNAPSHOT = datetime.fromisoformat("2026-11-13T12:00:00-06:00")


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/emulator" in str(item.fspath):
            item.add_marker(pytest.mark.emulator)


@pytest.fixture
def db(monkeypatch):
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        if os.environ.get("REQUIRE_EMULATOR") == "1":
            pytest.fail("REQUIRE_EMULATOR=1 but FIRESTORE_EMULATOR_HOST is not set")
        pytest.skip("Firestore emulator not configured")
    for k, v in VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FIRESTORE_TARGET", "emulator")
    get_settings.cache_clear()
    try:
        client = firestore_client(get_settings())
    except FirestoreTargetError as e:
        if os.environ.get("REQUIRE_EMULATOR") == "1":
            pytest.fail(str(e))
        pytest.skip(str(e))
    emulator.reset(client, _section_docs())
    yield client
    get_settings.cache_clear()


def _section_docs():
    """Minimal published policy_chunks so section validation runs in Firestore."""
    from google.cloud.firestore_v1.vector import Vector

    from app.policy_docs import load_policy_documents

    docs = {}
    for d in load_policy_documents():
        for sec in d.sections:
            docs[sec.section_id] = {
                "section_id": sec.section_id,
                "document_id": d.document_id,
                "property_id": d.property_id,
                "status": d.status,
                "embedding": Vector([0.0, 0.0, 1.0]),
            }
    return docs


@pytest.fixture
def clock():
    return FixedClock(SNAPSHOT)


@pytest.fixture
def service(db, clock):
    def make(ttl=900):
        return ProposalService(
            FirestoreProposalRepository(db),
            FirestoreWorkOrderRepository(db),
            FirestorePolicyStore(db),
            clock,
            ttl,
        )

    return make
