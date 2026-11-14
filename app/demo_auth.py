"""Demo authentication fixtures for local development and tests ONLY.

These fixtures stand in for a verified identity provider. They are not
credentials, are never seeded into Firestore, and are never indexed for
retrieval. Production code must build ``AuthContext`` from verified tokens.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.auth import AuthContext
from app.config import PROJECT_ROOT

DEMO_AUTH_PATH = PROJECT_ROOT / "data" / "fixtures" / "demo_auth_contexts.json"


def context_from_fixture(fixture: dict) -> AuthContext:
    return AuthContext(
        subject=fixture["subject"],
        role=fixture["role"],
        resident_id=fixture.get("resident_id"),
        allowed_property_ids=frozenset(fixture["allowed_property_ids"]),
        allowed_unit_ids=frozenset(fixture.get("allowed_unit_ids", [])),
    )


@lru_cache
def _load(path: Path) -> dict[str, AuthContext]:
    fixtures = json.loads(path.read_text())
    return {f["context_id"]: context_from_fixture(f) for f in fixtures}


def load_demo_context(context_id: str, path: Path = DEMO_AUTH_PATH) -> AuthContext:
    contexts = _load(path)
    try:
        return contexts[context_id]
    except KeyError:
        raise KeyError(
            f"unknown demo auth context {context_id!r}; known: {sorted(contexts)}"
        ) from None
