"""Failure injection at the release-critical filesystem boundaries."""

import asyncio
import json
import logging
import signal
from pathlib import Path

import pytest

from obsai.chunking import chunk_note
from obsai.config.models import EmbeddingConfig
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.errors import ParseError
from obsai.indexing import IncrementalIndexer, ShadowIndexRebuilder
from obsai.organizer.service import InboxOrganizer
from obsai.retrieval import FTSRetriever
from obsai.shutdown import ShutdownController, ShutdownRequested
from obsai.storage import Database, IndexRepository, SQLiteVectorStore
from obsai.transactions import TransactionOperation, TransactionService
from obsai.vault.parser import parse_markdown


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nOld body.\n", encoding="utf-8")
    database = tmp_path / "index.db"
    ShadowIndexRebuilder(vault, database).rebuild()
    return vault, database


def test_shadow_rebuild_replaces_valid_index_and_recovers_stale_build(tmp_path: Path) -> None:
    vault, database = _vault(tmp_path)
    (vault / "B.md").write_text("# B\n\nNew body.\n", encoding="utf-8")
    shadow = tmp_path / "index.db.building"
    shadow.write_bytes(b"stale incomplete build")
    result = ShadowIndexRebuilder(vault, database).rebuild()
    assert result.count("created") == 2
    assert not shadow.exists()
    with Database(database) as db:
        assert {row["path"] for row in db.connection.execute("SELECT path FROM notes")} == {"A.md", "B.md"}


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_rebuild_preserves_old_index(tmp_path: Path, monkeypatch, signum: int) -> None:
    vault, database = _vault(tmp_path)
    old_bytes = database.read_bytes()
    (vault / "B.md").write_text("# B\n\nNew body.\n", encoding="utf-8")
    original = IncrementalIndexer.update

    def interrupt(self, root):
        result = original(self, root)
        signal.raise_signal(signum)
        return result

    monkeypatch.setattr(IncrementalIndexer, "update", interrupt)
    with ShutdownController():
        with pytest.raises(ShutdownRequested) as captured:
            ShadowIndexRebuilder(vault, database).rebuild()
    assert captured.value.exit_code == 128 + signum
    assert database.read_bytes() == old_bytes
    assert not (tmp_path / "index.db.building").exists()
    with Database(database) as db:
        assert [row["path"] for row in db.connection.execute("SELECT path FROM notes")] == ["A.md"]


def test_rebuild_parse_and_swap_failures_preserve_old_index(tmp_path: Path, monkeypatch) -> None:
    import obsai.indexing.rebuild as rebuild_module

    vault, database = _vault(tmp_path)
    old_bytes = database.read_bytes()
    bad = vault / "bad.md"
    bad.write_text("---\nbroken: [\n", encoding="utf-8")
    with pytest.raises(ParseError):
        ShadowIndexRebuilder(vault, database).rebuild()
    assert database.read_bytes() == old_bytes
    assert not (tmp_path / "index.db.building").exists()
    bad.write_text("# Good\n\nBody.\n", encoding="utf-8")

    def disk_failure(source, destination):
        raise OSError("injected disk failure")

    monkeypatch.setattr(rebuild_module.os, "replace", disk_failure)
    with pytest.raises(OSError, match="disk failure"):
        ShadowIndexRebuilder(vault, database).rebuild()
    assert database.read_bytes() == old_bytes
    assert not (tmp_path / "index.db.building").exists()


def test_corrupt_live_database_can_be_rebuilt_from_vault(tmp_path: Path) -> None:
    vault, database = _vault(tmp_path)
    database.write_bytes(b"corrupt database")
    ShadowIndexRebuilder(vault, database).rebuild()
    with Database(database) as db:
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_concurrent_live_index_change_aborts_shadow_swap(tmp_path: Path, monkeypatch) -> None:
    vault, database = _vault(tmp_path)
    original = IncrementalIndexer.update

    def concurrent_update(self, root):
        result = original(self, root)
        with Database(database) as live:
            live.connection.execute("INSERT INTO index_state VALUES ('concurrent', 'yes', 'now')")
        return result

    monkeypatch.setattr(IncrementalIndexer, "update", concurrent_update)
    with pytest.raises(Exception, match="changed during rebuild"):
        ShadowIndexRebuilder(vault, database).rebuild()
    with Database(database) as db:
        assert db.connection.execute(
            "SELECT value FROM index_state WHERE key = 'concurrent'"
        ).fetchone()[0] == "yes"


def test_signal_during_transaction_rolls_back_applied_unit(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "A.md").write_bytes(b"A old")
    (tmp_path / "B.md").write_bytes(b"B old")
    service = TransactionService(tmp_path)
    plan = service.plan([
        TransactionOperation.replace("A.md", "old", "new"),
        TransactionOperation.replace("B.md", "old", "new"),
    ])
    original_apply = service.safe.apply
    calls = 0

    def interrupt_after_apply(change, *, approved):
        nonlocal calls
        original_apply(change, approved=approved)
        calls += 1
        if calls == 1:
            controller.request(signal.SIGINT)

    monkeypatch.setattr(service.safe, "apply", interrupt_after_apply)
    with ShutdownController() as controller:
        with pytest.raises(ShutdownRequested):
            service.execute(plan, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"A old"
    assert (tmp_path / "B.md").read_bytes() == b"B old"
    assert TransactionService.journals(tmp_path) == []


def test_signal_during_embedding_stops_next_batch_and_keeps_vectors_atomic(tmp_path: Path) -> None:
    class Provider:
        generation = EmbeddingGeneration("mock", "model", "v1", 3)

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            controller.request(signal.SIGINT)
            return [[1.0, 0.0, 0.0] for _ in texts]

    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        for name in ("A", "B"):
            note = parse_markdown(f"# {name}\n\nDistinct {name} body.\n", f"{name}.md")
            index.index_note(note, chunk_note(note))
        provider = Provider()
        pipeline = EmbeddingPipeline(SQLiteVectorStore(db), provider, EmbeddingConfig(
            batch_size=1, max_concurrency=1, price_per_million_tokens_usd=1.0,
        ))
        plan = pipeline.plan()
        assert plan.request_count == 2
        with ShutdownController() as controller:
            with controller.defer():
                with pytest.raises(ShutdownRequested):
                    asyncio.run(pipeline.execute(plan, approved=True))
        assert provider.calls == 1
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0


def test_signal_stops_organizer_before_proposing_more_notes(tmp_path: Path) -> None:
    vault, database = _vault(tmp_path)
    inbox = vault / "Inbox"
    inbox.mkdir()
    note = inbox / "captured.md"
    note.write_bytes(b"# Captured\n\nUntrusted content.\n")
    original_bytes = note.read_bytes()

    class Retriever:
        def search(self, query, limit=10, filters=None):
            return []

    with Database(database) as db:
        organizer = InboxOrganizer(vault, database, IndexRepository(db), Retriever())
        with ShutdownController() as controller:
            with controller.defer():
                controller.request(signal.SIGTERM)
                with pytest.raises(ShutdownRequested):
                    organizer.propose()
    assert note.read_bytes() == original_bytes


def test_structured_metrics_include_latency_and_counts_without_note_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault, database = _vault(tmp_path)
    with caplog.at_level(logging.INFO, logger="obsai.metrics"):
        with Database(database) as db:
            results = FTSRetriever(db).search("Old body")
            assert results
            class Provider:
                generation = EmbeddingGeneration("mock", "model", "v1", 3)

            EmbeddingPipeline(SQLiteVectorStore(db), Provider(), EmbeddingConfig(
                price_per_million_tokens_usd=1.0,
            )).plan()
    events = [json.loads(record.getMessage().removeprefix("metric "))
              for record in caplog.records if record.name == "obsai.metrics"]
    assert any(event["name"] == "retrieval.fts" and event["duration_ms"] >= 0
               for event in events)
    assert any(event["name"] == "embedding.plan" and event["tokens"] > 0
               and event["cache_hits"] == 0 for event in events)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Old body" not in logged
    assert str(vault) not in logged
    assert "A.md" not in logged
