"""Default retrieval pipeline with explicit semantic degradation reporting."""

import logging
from dataclasses import dataclass
from typing import Literal

from obsai.errors import EmbeddingError
from obsai.retrieval.fusion import rrf_fuse
from obsai.retrieval.models import Retriever, SearchFilters, SearchResult
from obsai.retrieval.reranker import NoOpReranker, Reranker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HybridOutcome:
    results: tuple[SearchResult, ...]
    warnings: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


class HybridRetriever:
    def __init__(
        self,
        keyword: Retriever,
        semantic: Retriever | None,
        *,
        candidate_limit: int = 20,
        rank_constant: int = 60,
        reranker: Reranker | None = None,
        on_semantic_failure: Literal["warn", "strict"] = "warn",
        semantic_unavailable_reason: str = "Semantic backend unavailable",
    ):
        if candidate_limit < 1 or rank_constant < 1:
            raise ValueError("Hybrid candidate limit and RRF constant must be positive")
        if on_semantic_failure not in ("warn", "strict"):
            raise ValueError("Unknown semantic failure policy")
        self.keyword = keyword
        self.semantic = semantic
        self.candidate_limit = candidate_limit
        self.rank_constant = rank_constant
        self.reranker = reranker or NoOpReranker()
        self.on_semantic_failure = on_semantic_failure
        self.semantic_unavailable_reason = semantic_unavailable_reason
        self.last_warnings: tuple[str, ...] = ()

    def search_with_status(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> HybridOutcome:
        self.last_warnings = ()
        if not query.strip() or limit <= 0:
            return HybridOutcome(())
        candidates = max(limit, self.candidate_limit)
        keyword_results = self.keyword.search(query, limit=candidates, filters=filters)
        vector_results: list[SearchResult] = []
        warnings: list[str] = []
        if self.semantic is None:
            warning = self.semantic_unavailable_reason
            if self.on_semantic_failure == "strict":
                raise EmbeddingError(warning)
            warnings.append(warning)
        else:
            try:
                vector_results = self.semantic.search(query, limit=candidates, filters=filters)
            except Exception as exc:
                if self.on_semantic_failure == "strict":
                    raise
                warnings.append(
                    f"Semantic backend failed ({type(exc).__name__}: {exc}); using keyword results"
                )
        for warning in warnings:
            logger.warning("Hybrid retrieval degraded: %s", warning)
        fused = rrf_fuse(
            {"keyword": keyword_results, "semantic": vector_results},
            rank_constant=self.rank_constant,
        )
        results = tuple(self.reranker.rerank(query, fused, limit)[:limit])
        self.last_warnings = tuple(warnings)
        return HybridOutcome(results, self.last_warnings)

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        return list(self.search_with_status(query, limit, filters).results)
