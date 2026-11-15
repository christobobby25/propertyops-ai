"""Firestore emulator helpers: reset and seed. Refuses to touch non-emulator clients."""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime

from app.agent.proposals import COUNTERS, WORK_ORDER_COUNTER
from app.gcp import EMULATOR_DATABASE, FirestoreTargetError, check_emulator_reachable, emulator_host
from app.seed_data import OPERATIONAL_COLLECTIONS, load_seed_collection


def _require_emulator(db) -> str:
    host = emulator_host()
    if not host:
        raise FirestoreTargetError("FIRESTORE_EMULATOR_HOST is not set")
    if not str(db.project).startswith("demo-"):
        raise FirestoreTargetError(f"refusing to reset non-demo project {db.project!r}")
    check_emulator_reachable(host)
    return host


def clear(db) -> None:
    """Delete every document in the emulator database (emulator REST API)."""
    host = _require_emulator(db)
    url = f"http://{host}/emulator/v1/projects/{db.project}/databases/{EMULATOR_DATABASE}/documents"
    with urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=10):
        pass


def next_work_order_number(work_orders: list[dict]) -> int:
    return max((int(w["work_order_id"].split("-")[1]) for w in work_orders), default=0) + 1


def seed_operational(db) -> None:
    _require_emulator(db)
    batch = db.batch()
    for collection, id_field in OPERATIONAL_COLLECTIONS.items():
        for record in load_seed_collection(collection):
            batch.set(db.collection(collection).document(record[id_field]), record)
    batch.set(
        db.collection(COUNTERS).document(WORK_ORDER_COUNTER),
        {"next": next_work_order_number(load_seed_collection("work_orders"))},
    )
    batch.commit()


def seed_policy_chunks(db, chunk_docs: dict[str, dict]) -> None:
    _require_emulator(db)
    batch = db.batch()
    for chunk_id, doc in chunk_docs.items():
        batch.set(db.collection("policy_chunks").document(chunk_id), doc)
    batch.commit()


def build_policy_chunk_docs(settings, embedder) -> dict[str, dict]:
    """Embed the local policy Markdown (live embedding calls) into chunk documents."""
    from app.gcp import gcs_uri
    from app.indexing import chunk_document
    from app.policy_docs import build_chunks, load_policy_documents

    docs = load_policy_documents()
    chunks = build_chunks(docs, {d.document_id: gcs_uri(settings, d.filename) for d in docs})
    vectors = embedder.embed([c.embedding_text for c in chunks], "RETRIEVAL_DOCUMENT")
    now = datetime.now(UTC)
    return {
        c.chunk_id: chunk_document(
            c,
            v,
            settings,
            c.content_hash(settings.embedding_model, settings.embedding_dimensions),
            now,
        )
        for c, v in zip(chunks, vectors, strict=True)
    }


def reset(db, chunk_docs: dict[str, dict] | None = None) -> None:
    """Fresh, independent dataset: clear, seed operational data (+ counter), and chunks."""
    clear(db)
    seed_operational(db)
    if chunk_docs:
        seed_policy_chunks(db, chunk_docs)
