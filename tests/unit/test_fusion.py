import pytest

from obsai.retrieval import HybridRetriever, SearchResult, rrf_fuse
from obsai.retrieval.evaluation import BenchmarkCase, evaluate


def result(chunk_id: str, source: str, path: str | None = None) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id, note_id=chunk_id, path=path or f"{chunk_id}.md",
        title=chunk_id, heading_path=[], snippet="text", score=1.0, source=source,
    )


class StaticRetriever:
    def __init__(self, results=None, error: Exception | None = None):
        self.results = results or []
        self.error = error
        self.calls = []

    def search(self, query, limit=10, filters=None):
        self.calls.append((query, limit, filters))
        if self.error:
            raise self.error
        return self.results[:limit]


def test_rrf_deduplicates_and_is_deterministic() -> None:
    keyword = [result("a", "keyword"), result("b", "keyword")]
    semantic = [result("b", "semantic"), result("a", "semantic"), result("b", "semantic")]
    first = rrf_fuse({"keyword": keyword, "semantic": semantic})
    second = rrf_fuse({"semantic": semantic, "keyword": keyword})
    assert [item.chunk_id for item in first] == ["a", "b"]
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]
    assert first[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert first[0].source == "hybrid"
    assert first[0].sources == ("keyword", "semantic")
    assert rrf_fuse({"keyword": [], "semantic": []}) == []
    with pytest.raises(ValueError):
        rrf_fuse({}, rank_constant=0)


def test_hybrid_vector_only_empty_and_failure_policy(caplog) -> None:
    keyword = StaticRetriever()
    semantic = StaticRetriever([result("target", "semantic")])
    retriever = HybridRetriever(keyword, semantic, candidate_limit=3)
    outcome = retriever.search_with_status("question", limit=1)
    assert [item.chunk_id for item in outcome.results] == ["target"]
    assert not outcome.degraded
    assert keyword.calls[0][1] == semantic.calls[0][1] == 3
    assert retriever.search("") == []
    assert len(keyword.calls) == 1

    failed = HybridRetriever(
        StaticRetriever([result("fallback", "keyword")]),
        StaticRetriever(error=RuntimeError("backend down")),
    )
    degraded = failed.search_with_status("question")
    assert [item.chunk_id for item in degraded.results] == ["fallback"]
    assert degraded.degraded
    assert "RuntimeError" in degraded.warnings[0]
    assert "backend down" in caplog.text
    assert failed.last_warnings == degraded.warnings
    with pytest.raises(RuntimeError, match="backend down"):
        HybridRetriever(
            StaticRetriever(), StaticRetriever(error=RuntimeError("backend down")),
            on_semantic_failure="strict",
        ).search("question")
    missing = HybridRetriever(StaticRetriever([result("fallback", "keyword")]), None)
    assert missing.search_with_status("question").degraded


def test_reranker_hook_and_evaluation_metrics() -> None:
    class ReverseReranker:
        def rerank(self, query, results, limit):
            return list(reversed(results))[:limit]

    retriever = HybridRetriever(
        StaticRetriever([result("a", "keyword"), result("b", "keyword")]),
        StaticRetriever(), reranker=ReverseReranker(),
    )
    assert [item.chunk_id for item in retriever.search("q", limit=1)] == ["b"]

    metric = evaluate(
        StaticRetriever([result("wrong", "keyword"), result("right", "keyword")]),
        [BenchmarkCase("q", expected_paths=("right.md",))], k=2,
    )
    assert metric.recall_at_k == 1.0
    assert metric.mrr == 0.5
    assert metric.precision_at_k == 0.5
    with pytest.raises(ValueError):
        evaluate(StaticRetriever(), [], k=2)
