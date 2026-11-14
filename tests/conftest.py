import pytest

from app import config

VALID_ENV = {
    "GOOGLE_GENAI_USE_VERTEXAI": "true",
    "GOOGLE_CLOUD_PROJECT": "test-project",
    "GOOGLE_CLOUD_LOCATION": "global",
    "GEMINI_MODEL": "gemini-test",
    "GOOGLE_CLOUD_REGION": "us-central1",
    "GCS_BUCKET": "test-bucket",
    "GCS_POLICY_PREFIX": "policy_docs/",
    "FIRESTORE_DATABASE": "(default)",
    "EMBEDDING_MODEL": "embed-test",
    "EMBEDDING_DIMENSIONS": "8",
}


@pytest.fixture
def valid_env(monkeypatch):
    """Isolated, valid settings that never depend on a local .env."""
    for k, v in VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
