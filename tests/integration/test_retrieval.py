import json
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obsai.chunking import chunk_note
from obsai.cli.app import app
from obsai.retrieval import FTSRetriever, Retriever, SearchFilters
from obsai.storage import Database, IndexRepository
from obsai.storage.schema import SCHEMA_V1
from obsai.vault.parser import parse_markdown


def add_note(index: IndexRepository, path: str, text: str) -> str:
    note = parse_markdown(text, path)
    return index.index_note(note, chunk_note(note))


def test_keyword_phrase_code_symbol_unicode_and_punctuation(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        first_id = add_note(
            index,
            "Go/context.md",
            "---\ntags: [go, backend]\n---\n# Go Context\n\n## Graceful Shutdown\n\n"
            "Call context.WithTimeout(ctx, 5*time.Second) for graceful shutdown.\n",
        )
        add_note(index, "Go/other.md", "# Other\n\nGraceful handling precedes shutdown.\n")
        chinese_id = add_note(index, "CN/lifecycle.md", "# 生命周期\n\n用于控制生命周期。\n")
        retriever: Retriever = FTSRetriever(db)

        for query in ("context.WithTimeout", "graceful shutdown", "`context.WithTimeout`"):
            found = retriever.search(query)
            assert found and found[0].note_id == first_id
            assert found[0].path == "Go/context.md"
            assert found[0].source == "keyword"
            assert found[0].chunk_id
            assert found[0].heading_path == ["Go Context", "Graceful Shutdown"]
            assert "context.WithTimeout" in found[0].snippet
        assert [result.note_id for result in retriever.search("生命周期")] == [chinese_id]
        assert retriever.search("`[[ ]]` ---") == []
        assert retriever.search("  ") == []
        assert retriever.search("context.WithTimeout", limit=0) == []
        assert len(retriever.search("shutdown", limit=1)) == 1


def test_title_heading_tags_filters_and_rename(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        tagged = add_note(index, "Backend/ops.md", "---\ntags: [urgent, go]\n---\n# Operations\n\n## Recovery Plan\n\nA note.\n")
        add_note(index, "Backend/other.md", "# Operations\n\nAnother note.\n")
        add_note(index, "Backendish/ops.md", "# Recovery Plan\n\nOther.\n")
        search = FTSRetriever(db).search
        assert search("Recovery Plan", filters=SearchFilters(tags=("urgent",)))[0].note_id == tagged
        assert [r.note_id for r in search("Operations", filters=SearchFilters(tags=("urgent", "go")))] == [tagged]
        assert [r.note_id for r in search("urgent")] == [tagged]
        assert search("Operations", filters=SearchFilters(tags=("missing",))) == []
        assert [r.path for r in search("Recovery Plan", filters=SearchFilters(folder="Backend"))] == ["Backend/ops.md"]
        assert [r.path for r in search("Recovery Plan", filters=SearchFilters(folder="Backend/"))] == ["Backend/ops.md"]
        before = search("Recovery Plan", filters=SearchFilters(tags=("urgent",)))[0]
        index.notes.update_path(tagged, "Archive/ops.md")
        after = search("Recovery Plan", filters=SearchFilters(folder="Archive"))[0]
        assert (after.note_id, after.chunk_id) == (before.note_id, before.chunk_id)
        assert search("Recovery Plan", filters=SearchFilters(folder="Backend", tags=("urgent",))) == []


def test_fts_tracks_modify_delete_clear_and_rollback(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        note_id = add_note(index, "A.md", "# Alpha\n\nOld keyword.\n")
        search = FTSRetriever(db).search
        assert search("Old keyword")
        changed = parse_markdown("# Alpha\n\nNew keyword.\n", "A.md")
        with pytest.raises(RuntimeError):
            with db.transaction():
                index.index_note(changed, chunk_note(changed))
                assert search("New keyword")
                raise RuntimeError("abort")
        assert search("Old keyword")
        assert search("New keyword") == []
        index.index_note(changed, chunk_note(changed))
        assert search("Old keyword") == []
        assert search("New keyword")
        replacement = parse_markdown("# Alpha\n\nStandalone replacement.\n", "A.md")
        index.chunks.replace_for_note(note_id, chunk_note(replacement))
        assert search("New keyword") == []
        assert search("Standalone replacement")
        index.notes.delete(note_id)
        assert search("Standalone replacement") == []
        add_note(index, "B.md", "# Beta\n\nClear this.\n")
        index.clear()
        assert search("Clear this") == []
        assert db.connection.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0] == 0


def test_v1_migration_backfills_existing_chunks(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    for statement in SCHEMA_V1:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("n1", "Legacy/a.md", "Legacy", "t", "t", "t", "hash", "{}", "{}", "{}"),
    )
    connection.execute(
        "INSERT INTO chunks (id, note_id, heading_path, raw_content, embedding_text, "
        "content_hash, embedding_text_hash, token_count, position, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c1", "n1", '["Historical"]', "context.WithTimeout legacy", "x", "h", "h", 1, 0, "{}"),
    )
    connection.execute("INSERT INTO tags VALUES (?, ?)", ("n1", "archive"))
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        result = FTSRetriever(db).search("context.WithTimeout", filters=SearchFilters(tags=("archive",)))
        assert [(r.note_id, r.chunk_id) for r in result] == [("n1", "c1")]
    with Database(path) as db:
        assert FTSRetriever(db).search("Historical")


def test_search_cli_keyword_filters_json_and_no_real_home(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Go").mkdir()
    (vault / "Go" / "context.md").write_text(
        "---\ntags: [go]\n---\n# Context\n\nUse context.WithTimeout.\n", encoding="utf-8"
    )
    db_path = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{db_path}"\n', encoding="utf-8"
    )
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    result = runner.invoke(app, ["search", "context.WithTimeout", "--mode", "keyword", "--tag", "go", "--folder", "Go", "--limit", "1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["path"] == "Go/context.md"
    assert payload[0]["source"] == "keyword"
    assert "Context" in runner.invoke(app, ["search", "context.WithTimeout"]).output
    assert json.loads(runner.invoke(app, ["search", "context.WithTimeout", "--mode", "keyword", "--tag", "missing", "--json"]).output) == []
    assert runner.invoke(app, ["search", "x", "--mode", "semantic"]).exit_code != 0


def test_incremental_update_keeps_fts_in_sync(tmp_path: Path) -> None:
    from obsai.indexing import IncrementalIndexer

    vault = tmp_path / "vault"
    vault.mkdir()
    path = vault / "A.md"
    path.write_text("# Stable\n\nUniqueOriginal.\n", encoding="utf-8")
    with Database(tmp_path / "index.db") as db:
        updater = IncrementalIndexer(IndexRepository(db))
        search = FTSRetriever(db).search
        updater.update(vault)
        original = search("UniqueOriginal")[0]
        path = path.rename(vault / "B.md")
        updater.update(vault)
        renamed = search("UniqueOriginal")[0]
        assert (renamed.note_id, renamed.chunk_id) == (original.note_id, original.chunk_id)
        assert renamed.path == "B.md"
        path.write_text("# Stable\n\nUniqueChanged.\n", encoding="utf-8")
        updater.update(vault)
        assert search("UniqueOriginal") == []
        assert search("UniqueChanged")[0].note_id == original.note_id
        path.unlink()
        updater.update(vault)
        assert search("UniqueChanged") == []


def test_small_synthetic_search_p95(tmp_path: Path) -> None:
    """A smoke benchmark; the 10k/100k production target needs separate profiling."""
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        for number in range(200):
            add_note(index, f"Synthetic/{number}.md", f"# Item {number}\n\nShared keyword {number}.\n")
        search = FTSRetriever(db).search
        durations = []
        for _ in range(50):
            start = time.perf_counter()
            assert len(search("Shared keyword", limit=10)) == 10
            durations.append(time.perf_counter() - start)
        p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
        assert p95 < 0.1, f"synthetic P95: {p95:.3f}s"
