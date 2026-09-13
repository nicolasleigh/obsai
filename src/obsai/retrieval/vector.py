"""Semantic retriever using the same SearchResult contract as FTS."""

import asyncio

from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.storage.vectors import SQLiteVectorStore


class VectorRetriever:
    def __init__(
        self, store: SQLiteVectorStore, pipeline: EmbeddingPipeline, *, approved: bool = False
    ):
        self.store = store
        self.pipeline = pipeline
        self.approved = approved

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        if not query.strip() or limit <= 0:
            return []
        vector = asyncio.run(self.pipeline.embed_query(query, approved=self.approved))
        return self.store.search(self.pipeline.generation, vector, limit, filters)
