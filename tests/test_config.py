import pytest
from pydantic import ValidationError

from app.config import ConfigError, get_settings


def test_loads_valid_settings(valid_env):
    s = get_settings()
    assert s.use_vertexai is True
    assert s.project == "test-project"
    assert s.location == "global"
    assert s.model == "gemini-test"


def test_settings_are_cached(valid_env):
    assert get_settings() is get_settings()


def test_settings_are_immutable(valid_env):
    s = get_settings()
    with pytest.raises(ValidationError):
        s.model = "other"


def test_strips_whitespace(valid_env):
    valid_env.setenv("GEMINI_MODEL", "  gemini-test  ")
    assert get_settings().model == "gemini-test"


@pytest.mark.parametrize("var", ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GEMINI_MODEL"])
def test_empty_value_rejected(valid_env, var):
    valid_env.setenv(var, "   ")
    with pytest.raises(ConfigError, match=var):
        get_settings()


def test_missing_value_rejected(valid_env):
    valid_env.delenv("GOOGLE_CLOUD_PROJECT")
    with pytest.raises(ConfigError, match="GOOGLE_CLOUD_PROJECT"):
        get_settings()


def test_placeholder_project_rejected(valid_env):
    valid_env.setenv("GOOGLE_CLOUD_PROJECT", "your-gcp-project-id")
    with pytest.raises(ConfigError, match="placeholder"):
        get_settings()


def test_reports_all_problems_at_once(valid_env):
    valid_env.setenv("GOOGLE_CLOUD_PROJECT", "")
    valid_env.setenv("GEMINI_MODEL", "")
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert "GOOGLE_CLOUD_PROJECT" in str(exc.value)
    assert "GEMINI_MODEL" in str(exc.value)


def test_invalid_bool_rejected(valid_env):
    valid_env.setenv("GOOGLE_GENAI_USE_VERTEXAI", "maybe")
    with pytest.raises(ConfigError, match="GOOGLE_GENAI_USE_VERTEXAI"):
        get_settings()


def test_data_settings_loaded(valid_env):
    s = get_settings()
    assert s.region == "us-central1"
    assert s.gcs_bucket == "test-bucket"
    assert s.firestore_database == "(default)"
    assert s.embedding_model == "embed-test"
    assert s.embedding_dimensions == 8


def test_optional_settings_have_defaults(valid_env):
    for var in ("GCS_POLICY_PREFIX", "FIRESTORE_DATABASE", "EMBEDDING_DIMENSIONS"):
        valid_env.delenv(var)
    s = get_settings()
    assert (s.gcs_policy_prefix, s.firestore_database, s.embedding_dimensions) == (
        "policy_docs/",
        "(default)",
        768,
    )


@pytest.mark.parametrize("var", ["GCS_BUCKET", "EMBEDDING_MODEL", "GOOGLE_CLOUD_REGION"])
def test_required_data_settings(valid_env, var):
    valid_env.delenv(var)
    with pytest.raises(ConfigError, match=var):
        get_settings()


@pytest.mark.parametrize("dims", ["0", "2049", "abc"])
def test_embedding_dimensions_bounds(valid_env, dims):
    valid_env.setenv("EMBEDDING_DIMENSIONS", dims)
    with pytest.raises(ConfigError, match="EMBEDDING_DIMENSIONS"):
        get_settings()


@pytest.mark.parametrize("bucket", ["gs://test-bucket", "test-bucket/docs"])
def test_bucket_must_be_bare_name(valid_env, bucket):
    valid_env.setenv("GCS_BUCKET", bucket)
    with pytest.raises(ConfigError, match="GCS_BUCKET"):
        get_settings()


@pytest.mark.parametrize("prefix", ["policy_docs", "/policy_docs/", "policy_docs//"])
def test_prefix_normalized(valid_env, prefix):
    valid_env.setenv("GCS_POLICY_PREFIX", prefix)
    assert get_settings().gcs_policy_prefix == "policy_docs/"
