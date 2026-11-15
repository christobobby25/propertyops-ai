"""Load and validate application settings in one place."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # Agent runtime. Anything other than "development" disables demo identities.
    app_env: Literal["development", "production"] = "production"
    dev_auth_context: str | None = None
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    agent_run_timeout_seconds: float = Field(default=180.0, gt=0, le=1800)
    max_llm_calls: int = Field(default=8, ge=1, le=50)
    max_tool_calls: int = Field(default=6, ge=1, le=50)
    model_retry_attempts: int = Field(default=3, ge=1, le=8)
    eval_judge_model: str | None = None

    # Work-order creation (Stage 2). Off by default: the agent never writes
    # unless explicitly enabled.
    enable_work_order_creation: bool = False
    proposal_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    # Firestore target. "emulator" requires FIRESTORE_EMULATOR_HOST and never
    # falls back to cloud; its project ID must be a demo-* project.
    firestore_target: Literal["cloud", "emulator"] = "cloud"
    emulator_project_id: str = "demo-propertyops"

    # Web app authentication. "demo" (header-selected fixtures) is refused unless
    # APP_ENV=development; "google_id_token" verifies a Google-signed ID token.
    auth_mode: Literal["demo", "google_id_token"] = "google_id_token"
    oauth_audience: str | None = None

    @field_validator("oauth_audience", mode="before")
    @classmethod
    def _blank_audience(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("emulator_project_id")
    @classmethod
    def _demo_project(cls, v: str) -> str:
        if not v.startswith("demo-"):
            raise ValueError("emulator project IDs must start with 'demo-'")
        return v

    @field_validator("dev_auth_context", "eval_judge_model", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def judge_model(self) -> str:
        return self.eval_judge_model or self.model

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
    "app_env": "APP_ENV",
    "dev_auth_context": "DEV_AUTH_CONTEXT",
    "tool_timeout_seconds": "TOOL_TIMEOUT_SECONDS",
    "model_timeout_seconds": "MODEL_TIMEOUT_SECONDS",
    "agent_run_timeout_seconds": "AGENT_RUN_TIMEOUT_SECONDS",
    "max_llm_calls": "MAX_LLM_CALLS",
    "max_tool_calls": "MAX_TOOL_CALLS",
    "model_retry_attempts": "MODEL_RETRY_ATTEMPTS",
    "eval_judge_model": "EVAL_JUDGE_MODEL",
    "enable_work_order_creation": "ENABLE_WORK_ORDER_CREATION",
    "proposal_ttl_seconds": "PROPOSAL_TTL_SECONDS",
    "firestore_target": "FIRESTORE_TARGET",
    "emulator_project_id": "EMULATOR_PROJECT_ID",
    "auth_mode": "AUTH_MODE",
    "oauth_audience": "OAUTH_AUDIENCE",
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
