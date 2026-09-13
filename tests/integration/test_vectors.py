import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obsai.chunking import chunk_note
from obsai.cli.app import app
from obsai.config.models import EmbeddingConfig
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.errors import EmbeddingBudgetError, EmbeddingError, EmbeddingRateLimitError, EmbeddingServiceError
from obsai.retrieval import SearchFilters, VectorRetriever
from obsai.storage import Database, IndexRepository, SQLiteVectorStore
from obsai.vault.parser import parse_markdown


class MockProvider:
    def __init__(self, generation: EmbeddingGeneration, outcomes=None):
        self.generation = generation
        self.outcomes = list(outcomes or [])
        self.calls: list[list[str]] = []
        self.active = 0
        self.max_active = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self.outcomes:
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return [
                [1.0, 0.0, 0.0] if ("Graceful Shutdown" in text or "服务怎么平滑退出" in text)
                else [0.0, 1.0, 0.0]
                for text in texts
            ]
        finally:
            self.active -= 1


def generation(version: str = "v1", dimensions: int = 3) -> EmbeddingGeneration:
    return EmbeddingGeneration("mock", "mock-model", version, dimensions)


def config(**changes) -> EmbeddingConfig:
    return EmbeddingConfig(price_per_million_tokens_usd=1.0, **changes)


def add(index: IndexRepository, path: str, body: str) -> str:
    note = parse_markdown(body, path)
    return index.index_note(note, chunk_note(note))


def test_semantic_search_and_generation_isolation(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        target = add(index, "Go/shutdown.md", "---\ntags: [operations]\n---\n# Go\n\n## Graceful Shutdown\n\nFinish requests.\n")
        add(index, "Go/other.md", "# Other\n\nUnrelated topic.\n")
        store = SQLiteVectorStore(db)
        provider = MockProvider(generation())
        pipeline = EmbeddingPipeline(store, provider, config(batch_size=1, max_concurrency=2))
        plan = pipeline.plan()
        assert plan.chunks_requiring_embeddings == 2
        assert plan.request_count == 2
        assert plan.estimated_tokens > 0
        assert plan.estimated_cost_usd > 0
        with pytest.raises(EmbeddingError, match="approval"):
            asyncio.run(pipeline.execute(plan))
        assert asyncio.run(pipeline.execute(plan, approved=True)) == 2
        assert len(provider.calls) == 2
        assert provider.max_active == 2

        results = VectorRetriever(store, pipeline, approved=True).search("服务怎么平滑退出？")
        assert results[0].note_id == target
        assert results[0].heading_path == ["Go", "Graceful Shutdown"]
        assert results[0].source == "semantic"
        assert [r.path for r in VectorRetriever(store, pipeline, approved=True).search(
            "服务怎么平滑退出？", filters=SearchFilters(folder="Go", tags=("operations",))
        )][0] == "Go/shutdown.md"
        assert VectorRetriever(store, pipeline, approved=True).search(
            "服务怎么平滑退出？", filters=SearchFilters(tags=("missing",))
        ) == []
        with pytest.raises(EmbeddingError, match="approval"):
            VectorRetriever(store, pipeline).search("服务怎么平滑退出？")

        next_generation = generation("v2")
        next_pipeline = EmbeddingPipeline(store, MockProvider(next_generation), config())
        assert next_pipeline.plan().chunks_requiring_embeddings == 2
        assert store.search(next_generation, [1.0, 0.0, 0.0]) == []
        assert store.search(generation(), [1.0, 0.0, 0.0])[0].note_id == target
        assert generation().cache_key("abc") != next_generation.cache_key("abc")
        smaller = generation("v1", 2)
        chunk = index.chunks.list_for_note(target)[0]
        store.upsert(smaller, chunk.chunk_id, chunk.embedding_text_hash, [1.0, 0.0], 10)
        assert store.search(smaller, [1.0, 0.0])[0].note_id == target
        assert smaller.id != generation().id
        with pytest.raises(EmbeddingError, match="cache key"):
            store.upsert(
                generation(), chunk.chunk_id, chunk.embedding_text_hash,
                [0.0, 1.0, 0.0], 10,
            )


def test_cache_hit_rename_modify_and_delete(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        first = add(index, "A.md", "# Shared\n\nSame body.\n")
        second = add(index, "B.md", "# Shared\n\nSame body.\n")
        store = SQLiteVectorStore(db)
        provider = MockProvider(generation())
        pipeline = EmbeddingPipeline(store, provider, config())
        plan = pipeline.plan()
        assert plan.cache_hits == 1
        assert plan.chunks_requiring_embeddings == 1
        assert len(plan.remote_texts) == 1
        assert asyncio.run(pipeline.execute(plan, approved=True)) == 2
        assert len(provider.calls) == 1
        assert pipeline.plan().request_count == 0

        first_chunk = index.chunks.list_for_note(first)[0]
        index.notes.update_path(first, "Moved/A.md")
        assert pipeline.plan().pending_chunks == ()
        assert store.search(generation(), [0.0, 1.0, 0.0])[0].path in {"Moved/A.md", "B.md"}
        assert index.chunks.list_for_note(first)[0].chunk_id == first_chunk.chunk_id

        index.notes.delete(second)
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 1
        changed = parse_markdown("# Shared\n\nChanged body.\n", "Moved/A.md")
        index.index_note(changed, chunk_note(changed), note_id=first)
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        assert pipeline.plan().request_count == 1
        add(index, "C.md", "# Shared\n\nSame body.\n")
        after_recreate = pipeline.plan()
        assert after_recreate.cache_hits == 1
        index.clear()
        assert db.connection.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0] == 0
        assert db.connection.execute("SELECT COUNT(*) FROM embedding_generations").fetchone()[0] == 0


@pytest.mark.parametrize("outcome", [
    EmbeddingRateLimitError("429"), TimeoutError("timeout"),
    EmbeddingServiceError("500"), ConnectionError("network"),
])
def test_retries_transient_errors(tmp_path: Path, outcome: Exception) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        add(index, "A.md", "# Retry\n\nTry again.\n")
        provider = MockProvider(generation(), [outcome])
        delays = []

        async def no_sleep(delay: float) -> None:
            delays.append(delay)

        pipeline = EmbeddingPipeline(
            SQLiteVectorStore(db), provider, config(max_attempts=2),
            sleep=no_sleep, jitter=lambda: 0.5,
        )
        asyncio.run(pipeline.execute(pipeline.plan(), approved=True))
        assert len(provider.calls) == 2
        assert delays == [0.5]


def test_budget_and_request_limits_prevent_remote_calls(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        add(IndexRepository(db), "A.md", "# Budget\n\nSome words.\n")
        store = SQLiteVectorStore(db)
        for option in (
            {"max_embedding_tokens": 0},
            {"estimated_cost_limit_usd": 0},
            {"max_embedding_requests": 0},
        ):
            provider = MockProvider(generation())
            with pytest.raises(EmbeddingBudgetError):
                EmbeddingPipeline(store, provider, config(**option)).plan()
            assert provider.calls == []
        provider = MockProvider(generation(), [EmbeddingRateLimitError("429")])
        pipeline = EmbeddingPipeline(
            store, provider, config(max_embedding_requests=1, max_attempts=3),
            sleep=lambda delay: asyncio.sleep(0),
        )
        with pytest.raises(EmbeddingBudgetError, match="Actual"):
            asyncio.run(pipeline.execute(pipeline.plan(), approved=True))
        assert len(provider.calls) == 1
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0


def test_async_timeout_is_retried_and_bounded(tmp_path: Path) -> None:
    class SlowProvider(MockProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            await asyncio.sleep(0.02)
            return [[1.0, 0.0, 0.0] for _ in texts]

    with Database(tmp_path / "index.db") as db:
        add(IndexRepository(db), "A.md", "# Slow\n\nTimeout test.\n")
        provider = SlowProvider(generation())
        pipeline = EmbeddingPipeline(
            SQLiteVectorStore(db), provider,
            config(timeout_seconds=0.001, max_attempts=2),
            sleep=lambda delay: asyncio.sleep(0),
        )
        with pytest.raises(TimeoutError):
            asyncio.run(pipeline.execute(pipeline.plan(), approved=True))
        assert len(provider.calls) == 2
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0


def test_dimension_mismatch_rejected_without_partial_write(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        note_id = add(index, "A.md", "# Vector\n\nData.\n")
        chunk = index.chunks.list_for_note(note_id)[0]
        store = SQLiteVectorStore(db)
        with pytest.raises(EmbeddingError, match="dimension"):
            store.upsert(generation(), chunk.chunk_id, chunk.embedding_text_hash, [1.0, 2.0], 4)
        provider = MockProvider(generation(), [[[1.0, 2.0]]])
        pipeline = EmbeddingPipeline(store, provider, config())
        with pytest.raises(EmbeddingError, match="dimension"):
            asyncio.run(pipeline.execute(pipeline.plan(), approved=True))
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0


def test_batches_obey_request_and_input_token_limits(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        for number in range(3):
            add(index, f"{number}.md", f"# Item {number}\n\nDistinct content {number}.\n")
        store = SQLiteVectorStore(db)
        provider = MockProvider(generation())
        sizes = sorted(
            [len(row[0].encode("utf-8")) for row in db.connection.execute(
                "SELECT embedding_text FROM chunks"
            )], reverse=True
        )
        pipeline = EmbeddingPipeline(
            store, provider, config(batch_size=10, max_request_tokens=sum(sizes[:2]))
        )
        plan = pipeline.plan()
        assert [len(batch) for batch in plan.batches] == [2, 1]
        assert all(sum(item.tokens for item in batch) <= sum(sizes[:2]) for batch in plan.batches)
        asyncio.run(pipeline.execute(plan, approved=True))
        assert [len(call) for call in provider.calls] == [2, 1]
        with pytest.raises(EmbeddingError, match="per-input"):
            EmbeddingPipeline(
                store,
                MockProvider(generation("new")),
                config(max_input_tokens=1),
            )._batches([plan.remote_texts[0]])


def test_cli_preflight_consent_and_semantic_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from obsai.embedding.openai_provider import OpenAIEmbeddingProvider

    calls = []

    async def fake_embed(self, texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed", fake_embed)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "shutdown.md").write_text(
        "# Graceful Shutdown\n\nFinish requests.\n", encoding="utf-8"
    )
    db_path = tmp_path / "index.db"
    config_path = tmp_path / "config" / "obsai" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{db_path}"\n'
        '[embedding]\ndimensions = 3\nmax_attempts = 1\n', encoding="utf-8"
    )
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    declined = runner.invoke(app, ["index", "embeddings"], input="\n")
    assert declined.exit_code == 1
    assert "Chunks requiring embeddings: 1" in declined.output
    assert "Estimated cost:" in declined.output
    assert calls == []
    approved = runner.invoke(app, ["index", "embeddings"], input="y\n")
    assert approved.exit_code == 0, approved.output
    assert "Vectors attached: 1" in approved.output
    assert len(calls) == 1
    cached = runner.invoke(app, ["index", "embeddings"])
    assert cached.exit_code == 0, cached.output
    assert len(calls) == 1
    found = runner.invoke(app, ["search", "服务怎么平滑退出？", "--mode", "semantic", "--json"], input="y\n")
    assert found.exit_code == 0, found.output
    assert json.loads(found.stdout[found.stdout.index("["):])[0]["title"] == "Graceful Shutdown"
    assert len(calls) == 2
    combined = runner.invoke(app, ["search", "Graceful Shutdown", "--json"], input="y\n")
    assert combined.exit_code == 0, combined.output
    merged = json.loads(combined.stdout[combined.stdout.index("["):])
    assert merged[0]["source"] == "hybrid"
    assert merged[0]["sources"] == ["keyword", "semantic"]
    declined_query = runner.invoke(app, ["search", "Graceful Shutdown", "--json"], input="\n")
    assert declined_query.exit_code == 0
    assert "not approved" in declined_query.stderr
    assert json.loads(declined_query.stdout[declined_query.stdout.index("["):])[0]["sources"] == ["keyword"]
    async def fail_embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingServiceError("provider unavailable")

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed", fail_embed)
    failed = runner.invoke(app, ["search", "Graceful Shutdown", "--json"], input="y\n")
    assert failed.exit_code == 0, failed.output
    assert "provider unavailable" in failed.stderr
    assert json.loads(failed.stdout[failed.stdout.index("["):])[0]["sources"] == ["keyword"]
    strict = runner.invoke(app, ["search", "Graceful Shutdown", "--strict-semantic"], input="y\n")
    assert strict.exit_code != 0


def test_v2_database_migrates_without_losing_fts(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    with Database(path) as db:
        add(IndexRepository(db), "A.md", "# Existing\n\nSearchable text.\n")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE chunk_embeddings")
        connection.execute("DROP TABLE embedding_cache")
        connection.execute("DROP TABLE embedding_generations")
        connection.execute("PRAGMA user_version = 2")
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        from obsai.retrieval import FTSRetriever
        assert FTSRetriever(db).search("Searchable text")
