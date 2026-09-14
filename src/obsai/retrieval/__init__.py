"""Search contracts and local keyword retriever."""

from obsai.retrieval.models import Retriever, SearchFilters, SearchResult

__all__ = [
    "Retriever", "SearchFilters", "SearchResult", "FTSRetriever", "VectorRetriever",
    "HybridRetriever", "HybridOutcome", "GraphRetriever", "Reranker", "NoOpReranker", "rrf_fuse",
]


def __getattr__(name: str):
    if name == "FTSRetriever":
        from obsai.retrieval.fts import FTSRetriever
        return FTSRetriever
    if name == "VectorRetriever":
        from obsai.retrieval.vector import VectorRetriever
        return VectorRetriever
    if name in ("HybridRetriever", "HybridOutcome"):
        from obsai.retrieval.hybrid import HybridOutcome, HybridRetriever
        return {"HybridRetriever": HybridRetriever, "HybridOutcome": HybridOutcome}[name]
    if name == "GraphRetriever":
        from obsai.retrieval.graph import GraphRetriever
        return GraphRetriever
    if name in ("Reranker", "NoOpReranker"):
        from obsai.retrieval.reranker import NoOpReranker, Reranker
        return {"Reranker": Reranker, "NoOpReranker": NoOpReranker}[name]
    if name == "rrf_fuse":
        from obsai.retrieval.fusion import rrf_fuse
        return rrf_fuse
    raise AttributeError(name)
