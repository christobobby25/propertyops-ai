"""Google Cloud client factories and shared resource names.

Clients are imported lazily so that offline code paths (dry runs, tests) never
need credentials or network access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Settings

if TYPE_CHECKING:
    from google import genai
    from google.cloud import firestore, storage

POLICY_CHUNKS_COLLECTION = "policy_chunks"
EMBEDDING_FIELD = "embedding"


def storage_client(s: Settings) -> storage.Client:
    from google.cloud import storage

    return storage.Client(project=s.project)


def firestore_client(s: Settings) -> firestore.Client:
    from google.cloud import firestore

    return firestore.Client(project=s.project, database=s.firestore_database)


def embedding_genai_client(s: Settings) -> genai.Client:
    """Embeddings use the regional endpoint (GOOGLE_CLOUD_REGION)."""
    from google import genai

    return genai.Client(vertexai=True, project=s.project, location=s.region)


def gcs_uri(s: Settings, filename: str) -> str:
    return f"gs://{s.gcs_bucket}/{s.gcs_policy_prefix}{filename}"
