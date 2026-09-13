"""Embedding and vector storage contracts."""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from obsai.retrieval.models import SearchFilters, SearchResult


@dataclass(frozen=True)
class EmbeddingGeneration:
    provider: str
    model: str
    model_version: str
    dimensions: int

    def __post_init__(self) -> None:
        if not all((self.provider, self.model, self.model_version)) or self.dimensions < 1:
            raise ValueError("Embedding generation requires identity and positive dimensions")

    @property
    def id(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.provider, self.model, self.model_version, self.dimensions],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    def cache_key(self, embedding_text_hash: str) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.provider, self.model, self.model_version, self.dimensions,
                 embedding_text_hash],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()


class EmbeddingProvider(Protocol):
    generation: EmbeddingGeneration

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def upsert(
        self,
        generation: EmbeddingGeneration,
        chunk_id: str,
        embedding_text_hash: str,
        vector: list[float],
        token_count: int,
    ) -> None: ...

    def delete(self, generation: EmbeddingGeneration, chunk_id: str) -> None: ...

    def search(
        self,
        generation: EmbeddingGeneration,
        vector: list[float],
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]: ...
