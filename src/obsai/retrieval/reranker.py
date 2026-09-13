"""Optional reranking boundary; V1 keeps fused order."""

from typing import Protocol

from obsai.retrieval.models import SearchResult


class Reranker(Protocol):
    def rerank(
        self, query: str, results: list[SearchResult], limit: int
    ) -> list[SearchResult]: ...


class NoOpReranker:
    def rerank(
        self, query: str, results: list[SearchResult], limit: int
    ) -> list[SearchResult]:
        return results[:limit]
