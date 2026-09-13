"""Search contracts and local keyword retriever."""

from obsai.retrieval.models import Retriever, SearchFilters, SearchResult

__all__ = ["Retriever", "SearchFilters", "SearchResult", "FTSRetriever", "VectorRetriever"]


def __getattr__(name: str):
    if name == "FTSRetriever":
        from obsai.retrieval.fts import FTSRetriever
        return FTSRetriever
    if name == "VectorRetriever":
        from obsai.retrieval.vector import VectorRetriever
        return VectorRetriever
    raise AttributeError(name)
