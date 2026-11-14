"""Text embeddings via Vertex AI (configurable model and dimensions)."""

from __future__ import annotations

from typing import Literal, Protocol

from app.config import Settings

TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: list[str], task_type: TaskType) -> list[list[float]]: ...


class VertexEmbedder:
    def __init__(self, settings: Settings, client=None):
        from app.gcp import embedding_genai_client

        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._client = client or embedding_genai_client(settings)

    def embed(self, texts: list[str], task_type: TaskType) -> list[list[float]]:
        from google.genai import types

        config = types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=self.dimensions
        )
        vectors: list[list[float]] = []
        # One text per request: some embedding models reject multi-input batches.
        for text in texts:
            resp = self._client.models.embed_content(
                model=self.model, contents=[text], config=config
            )
            values = list(resp.embeddings[0].values)
            if len(values) != self.dimensions:
                raise ValueError(
                    f"{self.model} returned {len(values)} dims, expected {self.dimensions}"
                )
            vectors.append(values)
        return vectors
