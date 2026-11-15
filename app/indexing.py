"""Shared construction of `policy_chunks` documents (cloud indexer and emulator seeding)."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.gcp import EMBEDDING_FIELD
from app.policy_docs import PolicyChunk


def chunk_document(
    c: PolicyChunk, vector: list[float], s: Settings, content_hash: str, now: datetime
) -> dict:
    from google.cloud.firestore_v1.vector import Vector

    return {
        "chunk_id": c.chunk_id,
        "section_id": c.section_id,
        "document_id": c.document_id,
        "property_id": c.property_id,
        "title": c.title,
        "heading": c.heading,
        "text": c.text,
        "version": c.version,
        "effective_from": c.effective_from,
        "status": c.status,
        "audience": list(c.audience),
        "language": c.language,
        "source_uri": c.source_uri,
        "content_hash": content_hash,
        "embedding_model": s.embedding_model,
        "embedding_dimensions": s.embedding_dimensions,
        "indexed_at": now,
        EMBEDDING_FIELD: Vector(vector),
    }
