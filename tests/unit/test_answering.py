import asyncio

import pytest

from obsai.answering.context import ContextBuilder, estimate_tokens
from obsai.answering.models import EvidenceRecord
from obsai.answering.service import AskService
from obsai.config.models import AskConfig
from obsai.errors import ContextError, LLMError
from obsai.retrieval.hybrid import HybridOutcome
from obsai.retrieval.hybrid import HybridRetriever
from obsai.retrieval.models import SearchResult


def result(chunk_id: str, note_id: str = "n1") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id, note_id=note_id, path="stale.md", title="Stale",
        heading_path=[], snippet="truncated search snippet", score=1,
        source="hybrid",
    )


class Records:
    def __init__(self, records: list[EvidenceRecord]):
        self.records = {record.chunk_id: record for record in records}

    def get(self, chunk_id: str) -> EvidenceRecord | None:
        return self.records.get(chunk_id)


def record(chunk_id: str, content: str = "Graceful shutdown waits for requests.") -> EvidenceRecord:
    return EvidenceRecord(chunk_id, "n1", "Go/context.md", "Go Context", ("Shutdown",), "block-1", content)


class Retriever:
    def __init__(self, results: list[SearchResult]):
        self.results = results

    def search_with_status(self, query: str, *, limit: int, filters=None) -> HybridOutcome:
        return HybridOutcome(tuple(self.results[:limit]))


class Provider:
    def __init__(self, output: str = "It waits for requests. [S1]", error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = 0

    async def generate(self, system_prompt: str, user_prompt: str, *, max_output_tokens: int) -> str:
        self.calls += 1
        assert "Go/context.md" in user_prompt
        assert "truncated search snippet" not in user_prompt
        if self.error:
            raise self.error
        return self.output


def service(results: list[SearchResult], records: list[EvidenceRecord], llm_provider: Provider, **config) -> AskService:
    settings = AskConfig(**config)
    return AskService(
        Retriever(results), ContextBuilder(Records(records), settings), llm_provider, settings
    )


def test_context_budget_dedupe_order_and_metadata() -> None:
    settings = AskConfig(max_context_tokens=750, max_evidence_tokens=100, max_chunks=2)
    builder = ContextBuilder(Records([record("c1"), record("c2", "Another fact.")]), settings)
    context = builder.build("How?", [result("c1"), result("c1"), result("c2")])
    assert [item.citation_id for item in context.evidence] == ["S1", "S2"]
    assert context.evidence[0].record.path == "Go/context.md"
    assert context.evidence[0].record.heading_path == ("Shutdown",)
    assert context.evidence[0].record.block_id == "block-1"
    assert context.token_estimate == estimate_tokens(context.system_prompt + context.user_prompt)
    assert context.token_estimate <= settings.max_context_tokens


def test_very_long_chunk_is_bounded_and_marked() -> None:
    settings = AskConfig(max_context_tokens=700, max_evidence_tokens=80, max_chunks=1)
    context = ContextBuilder(Records([record("c1", "中文" * 1000)]), settings).build("问?", [result("c1")])
    assert len(context.evidence) == 1
    assert context.evidence[0].truncated
    assert "[excerpt truncated]" in context.evidence[0].content
    assert estimate_tokens(context.evidence[0].content) <= 80
    assert context.token_estimate <= 700


def test_too_small_prompt_budget_raises() -> None:
    with pytest.raises(ContextError):
        ContextBuilder(Records([]), AskConfig(max_context_tokens=10)).build("Question", [])


def test_answer_uses_only_cited_evidence() -> None:
    provider = Provider()
    answer = service([result("c1")], [record("c1")], provider).ask("How?")
    assert answer.text == "It waits for requests. [S1]"
    assert [source.record.path for source in answer.sources] == ["Go/context.md"]
    assert provider.calls == 1


@pytest.mark.parametrize("citation", ["【S1】", "（S1）", "(S1)"])
def test_local_citation_variants_are_normalized(citation: str) -> None:
    answer = service(
        [result("c1")], [record("c1")], Provider(f"事实 {citation}"), provider="ollama"
    ).ask("How?")
    assert answer.text == "事实 [S1]"
    assert [source.citation_id for source in answer.sources] == ["S1"]


def test_hidden_thinking_cannot_supply_the_only_citation() -> None:
    answer = service(
        [result("c1")],
        [record("c1")],
        Provider("<think>推理 [S1]</think>没有引用"),
        provider="ollama",
    ).ask("How?")
    assert answer.abstained
    assert not answer.sources


def test_sources_include_only_citations_actually_used() -> None:
    answer = (
        service(
            [result("c1"), result("c2")], [record("c1"), record("c2")],
            Provider("Only the second source matters. [S2] [S2]"),
        ).ask("How?")
    )
    assert [source.citation_id for source in answer.sources] == ["S2"]


@pytest.mark.parametrize("output", ["Invented [S99]", "No citations at all", "Mixed [S1] and [S99]", "Wrong [S0]"])
def test_invalid_citation_abstains(output: str) -> None:
    answer = service([result("c1")], [record("c1")], Provider(output)).ask("How?")
    assert answer.abstained
    assert not answer.sources
    assert "S99" not in answer.text


def test_zero_result_skips_provider() -> None:
    provider = Provider()
    answer = service([], [], provider).ask("How?")
    assert answer.abstained
    assert provider.calls == 0


def test_provider_error_is_domain_error() -> None:
    with pytest.raises(LLMError, match="Answer provider failed"):
        service([result("c1")], [record("c1")], Provider(error=TimeoutError())).ask("How?")


def test_ask_keeps_sync_semantic_retriever_usable() -> None:
    class Keyword:
        def search(self, query, limit=10, filters=None):
            return []

    class Semantic:
        def search(self, query, limit=10, filters=None):
            asyncio.run(asyncio.sleep(0))
            return [result("c1")]

    config = AskConfig()
    hybrid = HybridRetriever(Keyword(), Semantic())
    answer = AskService(
        hybrid, ContextBuilder(Records([record("c1")]), config), Provider(), config
    ).ask("How?")
    assert answer.sources[0].record.path == "Go/context.md"
    assert not answer.warnings
