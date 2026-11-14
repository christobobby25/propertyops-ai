"""Load and validate application settings in one place."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_PROJECT = "your-gcp-project-id"

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    use_vertexai: bool
    project: str
    location: str
    model: str

    @field_validator("project", "location", "model")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("project")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if v == PLACEHOLDER_PROJECT:
            raise ValueError("still set to the .env.example placeholder")
        return v


# Settings field -> environment variable
_ENV_VARS = {
    "use_vertexai": "GOOGLE_GENAI_USE_VERTEXAI",
    "project": "GOOGLE_CLOUD_PROJECT",
    "location": "GOOGLE_CLOUD_LOCATION",
    "model": "GEMINI_MODEL",
}


@lru_cache
def get_settings() -> Settings:
    raw = {field: os.environ.get(var, "") for field, var in _ENV_VARS.items()}
    try:
        return Settings(**raw)
    except ValidationError as e:
        problems = "\n".join(
            f"  - {_ENV_VARS[str(err['loc'][0])]}: {err['msg']}" for err in e.errors()
        )
        raise ConfigError(
            f"Invalid configuration (check {PROJECT_ROOT / '.env'}):\n{problems}"
        ) from None
