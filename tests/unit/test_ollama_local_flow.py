from __future__ import annotations

import asyncio

from obsai.application.embedding import build_embedding_pipeline
from obsai.application.search import probe_semantic
from obsai.config.models import EmbeddingConfig, Settings
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository, SQLiteVectorStore


class LocalProvider:
    generation = EmbeddingGeneration("ollama", "embed-local", "embed-local", 2)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def test_local_embedding_query_does_not_require_remote_approval(tmp_path) -> None:
    with Database(tmp_path / "index.db") as database:
        pipeline = EmbeddingPipeline(
            SQLiteVectorStore(database),
            LocalProvider(),
            EmbeddingConfig(provider="ollama", model="embed-local", dimensions=2),
        )

        assert asyncio.run(pipeline.embed_query("local query")) == [1.0, 0.0]


def test_local_semantic_probe_is_ready_without_consent(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nLocal content.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
        settings = Settings(
            vault={"path": vault},
            index={"database": database_path},
            embedding={
                "provider": "ollama",
                "model": "embed-local",
                "dimensions": 2,
            },
        )
        store, pipeline = build_embedding_pipeline(database, settings)
        store.ensure_generation(pipeline.generation)

    with Database(database_path) as database:
        probe = probe_semantic("local", database=database, settings=settings)

    assert probe.consent is None
    assert probe.reason == ""
    assert probe.failure is None
