"""Deterministic Reciprocal Rank Fusion independent of retriever backends."""

from collections.abc import Mapping, Sequence

from obsai.retrieval.models import SearchResult


def rrf_fuse(
    sources: Mapping[str, Sequence[SearchResult]], *, rank_constant: int = 60
) -> list[SearchResult]:
    if rank_constant < 1:
        raise ValueError("RRF rank constant must be positive")
    scores: dict[str, float] = {}
    representatives: dict[str, SearchResult] = {}
    provenance: dict[str, set[str]] = {}
    best_rank: dict[str, int] = {}

    # Sorting source names makes ties and representative selection independent
    # of the caller's mapping insertion order.
    for source in sorted(sources):
        seen: set[str] = set()
        for rank, result in enumerate(sources[source], start=1):
            if result.chunk_id in seen:
                continue
            seen.add(result.chunk_id)
            scores[result.chunk_id] = scores.get(result.chunk_id, 0.0) + 1.0 / (
                rank_constant + rank
            )
            representatives.setdefault(result.chunk_id, result)
            provenance.setdefault(result.chunk_id, set()).add(source)
            best_rank[result.chunk_id] = min(best_rank.get(result.chunk_id, rank), rank)

    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], best_rank[chunk_id], chunk_id))
    return [
        representatives[chunk_id].model_copy(update={
            "score": scores[chunk_id],
            "source": "hybrid",
            "sources": tuple(sorted(provenance[chunk_id])),
        })
        for chunk_id in ordered
    ]
