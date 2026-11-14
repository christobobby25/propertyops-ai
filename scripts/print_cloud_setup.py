"""Print (never run) the one-time gcloud commands this project needs.

    python -m scripts.print_cloud_setup

Covers: enabling the Firestore API, creating the Firestore database, and the
composite vector index used for property-filtered policy search. No RAG Engine
or dedicated vector-serving infrastructure is involved.
"""

from __future__ import annotations

import json
import shlex
import sys

from app.gcp import EMBEDDING_FIELD, POLICY_CHUNKS_COLLECTION
from scripts._cli import load_settings


def commands(s) -> list[tuple[str, str]]:
    p, db = shlex.quote(s.project), shlex.quote(s.firestore_database)
    vector_config = json.dumps({"dimension": str(s.embedding_dimensions), "flat": "{}"})
    return [
        (
            "Enable the Firestore API",
            f"gcloud services enable firestore.googleapis.com --project={p}",
        ),
        (
            "Create the Firestore database (Native mode)",
            f"gcloud firestore databases create --project={p} --database={db} "
            f"--location={shlex.quote(s.region)} --type=firestore-native",
        ),
        (
            "Create the property-filtered vector index on policy_chunks",
            f"gcloud firestore indexes composite create --project={p} --database={db} "
            f"--collection-group={POLICY_CHUNKS_COLLECTION} --query-scope=COLLECTION "
            "--field-config=field-path=property_id,order=ascending "
            "--field-config=field-path=status,order=ascending "
            f"--field-config=field-path={EMBEDDING_FIELD},vector-config={shlex.quote(vector_config)}",
        ),
        (
            "Check index build status (wait for READY)",
            f"gcloud firestore indexes composite list --project={p} --database={db}",
        ),
    ]


def main() -> int:
    s = load_settings()
    if s is None:
        return 2
    print("# One-time setup. Review, then run these yourself.\n")
    for i, (title, cmd) in enumerate(commands(s), 1):
        print(f"# {i}. {title}\n{cmd}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
