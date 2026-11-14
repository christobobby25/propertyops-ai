"""Load and validate application settings in one place."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_PROJECT = "your-gcp-project-id"
MAX_FIRESTORE_VECTOR_DIMENSIONS = 2048

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Gemini (generation)
    use_vertexai: bool
    project: str
    location: str
    model: str

    # Storage / data
    region: str
    gcs_bucket: str
    gcs_policy_prefix: str = "policy_docs/"
    firestore_database: str = "(default)"

    # Embeddings
    embedding_model: str
    embedding_dimensions: int = Field(default=768, ge=1, le=MAX_FIRESTORE_VECTOR_DIMENSIONS)

    @field_validator(
        "project",
        "location",
        "model",
        "region",
        "gcs_bucket",
        "firestore_database",
        "embedding_model",
    )
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

    @field_validator("gcs_bucket")
    @classmethod
    def _bare_bucket_name(cls, v: str) -> str:
        if v.startswith("gs://") or "/" in v:
            raise ValueError("use the bare bucket name, e.g. 'propertyops' (no gs://, no path)")
        return v

    @field_validator("gcs_policy_prefix")
    @classmethod
    def _normalize_prefix(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not v:
            raise ValueError("must not be empty")
        return v + "/"


# Settings field -> environment variable
_ENV_VARS = {
    "use_vertexai": "GOOGLE_GENAI_USE_VERTEXAI",
    "project": "GOOGLE_CLOUD_PROJECT",
    "location": "GOOGLE_CLOUD_LOCATION",
    "model": "GEMINI_MODEL",
    "region": "GOOGLE_CLOUD_REGION",
    "gcs_bucket": "GCS_BUCKET",
    "gcs_policy_prefix": "GCS_POLICY_PREFIX",
    "firestore_database": "FIRESTORE_DATABASE",
    "embedding_model": "EMBEDDING_MODEL",
    "embedding_dimensions": "EMBEDDING_DIMENSIONS",
}


@lru_cache
def get_settings() -> Settings:
    # Unset variables fall back to model defaults; required ones then fail validation.
    raw = {field: os.environ[var] for field, var in _ENV_VARS.items() if var in os.environ}
    try:
        return Settings(**raw)
    except ValidationError as e:
        problems = "\n".join(
            f"  - {_ENV_VARS[str(err['loc'][0])]}: {err['msg']}" for err in e.errors()
        )
        raise ConfigError(
            f"Invalid configuration (check {PROJECT_ROOT / '.env'}):\n{problems}"
        ) from None
