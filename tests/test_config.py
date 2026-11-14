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
