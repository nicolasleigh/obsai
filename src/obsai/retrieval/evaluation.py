"""Small, deterministic retrieval benchmark metrics."""

from dataclasses import dataclass
from typing import Iterable

from obsai.retrieval.models import Retriever


@dataclass(frozen=True)
class BenchmarkCase:
    query: str
    expected_paths: tuple[str, ...] = ()
    expected_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_k: float
    mrr: float
    precision_at_k: float


def evaluate(retriever: Retriever, cases: Iterable[BenchmarkCase], *, k: int = 5) -> RetrievalMetrics:
    if k < 1:
        raise ValueError("K must be positive")
    cases = tuple(cases)
    if not cases:
        raise ValueError("Benchmark requires at least one case")
    recalls: list[float] = []
    reciprocals: list[float] = []
    precisions: list[float] = []
    for case in cases:
        expected = {("path", path) for path in case.expected_paths} | {
            ("chunk", chunk_id) for chunk_id in case.expected_chunk_ids
        }
        if not expected:
            raise ValueError("Benchmark case needs an expected note path or chunk ID")
        found: set[tuple[str, str]] = set()
        first_rank = None
        relevant_results = 0
        for rank, result in enumerate(retriever.search(case.query, limit=k)[:k], start=1):
            matches = expected & {("path", result.path), ("chunk", result.chunk_id)}
            new_matches = matches - found
            if new_matches:
                found.update(new_matches)
                relevant_results += 1
                if first_rank is None:
                    first_rank = rank
        recalls.append(len(found) / len(expected))
        reciprocals.append(1.0 / first_rank if first_rank is not None else 0.0)
        precisions.append(relevant_results / k)
    count = len(cases)
    return RetrievalMetrics(
        recall_at_k=sum(recalls) / count,
        mrr=sum(reciprocals) / count,
        precision_at_k=sum(precisions) / count,
    )
