"""Property-scoped policy retrieval over Firestore vector search.

The property filter is decided here, in backend code, from a trusted
``AuthContext`` — never from a model- or user-supplied property ID alone.
Firestore applies the filter before nearest-neighbour ranking, and results
are re-checked here as defence in depth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.auth import AuthContext, authorize_property
from app.embeddings import Embedder
from app.gcp import EMBEDDING_FIELD, POLICY_CHUNKS_COLLECTION

DEFAULT_LIMIT = 4
MAX_LIMIT = 10


@dataclass(frozen=True)
class RetrievedPolicy:
    chunk_id: str
    document_id: str
    section_id: str
    property_id: str
    title: str
    heading: str
    text: str
    version: str
    effective_from: str
    source_uri: str
    distance: float | None


class PolicyVectorStore(Protocol):
    def find_nearest(
        self, *, property_id: str, query_vector: list[float], limit: int
    ) -> list[dict]: ...


class FirestorePolicyStore:
    """Requires a composite vector index on (property_id, status, embedding)."""

    def __init__(self, client, collection: str = POLICY_CHUNKS_COLLECTION):
        self._client = client
        self._collection = collection

    def find_nearest(
        self, *, property_id: str, query_vector: list[float], limit: int
    ) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("property_id", "==", property_id))
            .where(filter=FieldFilter("status", "==", "published"))
            .find_nearest(
                vector_field=EMBEDDING_FIELD,
                query_vector=Vector(query_vector),
                distance_measure=DistanceMeasure.COSINE,
                limit=limit,
                distance_result_field="vector_distance",
            )
        )
        return [{"chunk_id": snap.id, **snap.to_dict()} for snap in query.stream()]

    def published_sections(self, property_id: str, section_ids: list[str]) -> set[str]:
        """Section IDs that exist, are published, and belong to ``property_id``."""
        col = self._client.collection(self._collection)
        snaps = self._client.get_all([col.document(s) for s in section_ids], field_paths=None)
        return {
            s.id
            for s in snaps
            if s.exists and s.get("property_id") == property_id and s.get("status") == "published"
        }


class PolicyRetriever:
    def __init__(self, store: PolicyVectorStore, embedder: Embedder):
        self._store = store
        self._embedder = embedder

    def search(
        self,
        auth: AuthContext,
        query: str,
        *,
        property_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedPolicy]:
        """Search published policies the authenticated actor may read.

        ``property_id`` is an optional, untrusted hint; it is honoured only if
        it is within ``auth``'s allowed properties (see ``authorize_property``).
        """
        scoped_property = authorize_property(auth, property_id)
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(limit, MAX_LIMIT))

        [vector] = self._embedder.embed([query], task_type="RETRIEVAL_QUERY")
        rows = self._store.find_nearest(
            property_id=scoped_property, query_vector=vector, limit=limit
        )

        results = []
        for row in rows:
            # Defence in depth: never return anything outside the trusted scope.
            if row.get("property_id") != scoped_property:
                continue
            if row.get("status") != "published" or auth.role not in row.get("audience", ()):
                continue
            results.append(
                RetrievedPolicy(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    section_id=row["section_id"],
                    property_id=row["property_id"],
                    title=row["title"],
                    heading=row["heading"],
                    text=row["text"],
                    version=row["version"],
                    effective_from=row["effective_from"],
                    source_uri=row["source_uri"],
                    distance=row.get("vector_distance"),
                )
            )
        return results
