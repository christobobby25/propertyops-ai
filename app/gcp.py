"""Google Cloud client factories and shared resource names.

Clients are imported lazily so that offline code paths (dry runs, tests) never
need credentials or network access.
"""

from __future__ import annotations

import os
import socket
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


class FirestoreTargetError(RuntimeError):
    """Firestore target is misconfigured or unreachable. Never falls back."""


EMULATOR_HOST_VAR = "FIRESTORE_EMULATOR_HOST"
EMULATOR_DATABASE = "(default)"


def emulator_host() -> str | None:
    return os.environ.get(EMULATOR_HOST_VAR) or None


def check_emulator_reachable(host: str, timeout: float = 2.0) -> None:
    hostname, _, port = host.rpartition(":")
    if not hostname or not port.isdigit():
        raise FirestoreTargetError(f"{EMULATOR_HOST_VAR}={host!r} is not host:port")
    if hostname not in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        raise FirestoreTargetError(f"emulator host must be local, got {hostname!r}")
    try:
        with socket.create_connection((hostname.strip("[]"), int(port)), timeout=timeout):
            pass
    except OSError as e:
        raise FirestoreTargetError(f"Firestore emulator unreachable at {host}: {e}") from None


def firestore_client(s: Settings) -> firestore.Client:
    """Return a client for exactly the configured target; fail closed otherwise.

    * emulator: requires a reachable local FIRESTORE_EMULATOR_HOST, uses the
      demo-* project and anonymous credentials. Never touches cloud.
    * cloud: refuses to start if FIRESTORE_EMULATOR_HOST is set, because the
      client library would silently redirect to the emulator.
    """
    from google.cloud import firestore

    host = emulator_host()
    if s.firestore_target == "emulator":
        if not host:
            raise FirestoreTargetError(
                f"FIRESTORE_TARGET=emulator but {EMULATOR_HOST_VAR} is not set"
            )
        check_emulator_reachable(host)
        from google.auth.credentials import AnonymousCredentials

        return firestore.Client(
            project=s.emulator_project_id,
            database=EMULATOR_DATABASE,
            credentials=AnonymousCredentials(),
        )

    if host:
        raise FirestoreTargetError(
            f"{EMULATOR_HOST_VAR} is set but FIRESTORE_TARGET=cloud; refusing an ambiguous target"
        )
    return firestore.Client(project=s.project, database=s.firestore_database)


def embedding_genai_client(s: Settings) -> genai.Client:
    """Embeddings use the regional endpoint (GOOGLE_CLOUD_REGION)."""
    from google import genai

    return genai.Client(vertexai=True, project=s.project, location=s.region)


def gcs_uri(s: Settings, filename: str) -> str:
    return f"gs://{s.gcs_bucket}/{s.gcs_policy_prefix}{filename}"
