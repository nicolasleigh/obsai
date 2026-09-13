import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from obsai.chunking import chunk_note
from obsai.errors import SchemaError
from obsai.storage import ChunkRepository, Database, IndexRepository, NoteRepository
from obsai.vault.parser import parse_markdown


def sample_note(path: str = "Source.md", extra: str = ""):
    return parse_markdown(
        "---\ntitle: Source Title\ntags: [work, reference]\nstatus: active\n---\n"
        "# Source Title\n\nstatus:: done\n\nSee [[Target|Alias]] ^source-block\n"
        + extra,
        path,
    )


def test_schema_initialization_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"notes", "chunks", "tags", "links", "blocks", "index_state", "dirty_notes", "chunk_fts"} <= tables
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_full_parsed_note_and_chunks_roundtrip(tmp_path: Path) -> None:
    note = sample_note()
    chunks = chunk_note(note)
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        note_id = index.index_note(note, chunks)
        record = index.notes.get(note_id)
        assert record is not None
        assert record.id == note_id
        assert note_id != sha256(note.path.encode()).hexdigest()
        assert record.path == note.path
        assert record.title == note.title
        assert len(record.content_hash) == 64
        assert record.frontmatter == note.frontmatter
        assert record.dataview_fields == {"status": "done"}
        assert index.notes.get_parsed(note_id) == note
        assert index.tags_for_note(note_id) == ["reference", "work"]
        assert index.blocks_for_note(note_id) == note.blocks

        stored_chunks = index.chunks.list_for_note(note_id)
        assert len(stored_chunks) == len(chunks)
        assert [chunk.note_id for chunk in stored_chunks] == [note_id] * len(chunks)
        assert [chunk.raw_content for chunk in stored_chunks] == [chunk.raw_content for chunk in chunks]
        assert [chunk.embedding_text_hash for chunk in stored_chunks] == [
            chunk.embedding_text_hash for chunk in chunks
        ]
        assert stored_chunks[0].metadata["path"] == note.path
        for input_chunk, stored_chunk in zip(chunks, stored_chunks, strict=True):
            assert stored_chunk == input_chunk.model_copy(
                update={"chunk_id": stored_chunk.chunk_id, "note_id": note_id}
            )

        link = index.links_for_note(note_id)[0]
        assert link.source_block_id == "source-block"
        assert link.target_path == "Target"
        assert link.target_note_id is None
        assert link.display_text == "Alias"

    with Database(tmp_path / "index.db") as db:
        assert NoteRepository(db).get_parsed(note_id) == note
        assert ChunkRepository(db).list_for_note(note_id) == stored_chunks


def test_update_replaces_derived_rows_and_keeps_id(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        first = sample_note()
        note_id = index.index_note(first, chunk_note(first))
        before = index.notes.get(note_id)
        updated = sample_note(extra="\nA new paragraph with #fresh.\n")
        assert index.index_note(updated, chunk_note(updated)) == note_id
        after = index.notes.get(note_id)
        assert after is not None and before is not None
        assert after.created_at == before.created_at
        assert after.content_hash != before.content_hash
        assert index.notes.get_parsed(note_id) == updated
        assert index.tags_for_note(note_id) == ["fresh", "reference", "work"]
        assert len(index.chunks.list_for_note(note_id)) == len(chunk_note(updated))


def test_rename_keeps_note_and_chunk_ids_and_embedding_hash(tmp_path: Path) -> None:
    raw = "# Go Context\n\nContext 可以设置超时。\n"
    before = parse_markdown(raw, "Go/context.md")
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        note_id = index.index_note(before, chunk_note(before))
        original_chunk = index.chunks.list_for_note(note_id)[0]
        index.notes.update_path(note_id, "Backend/context.md")
        assert index.notes.get_by_path("Go/context.md") is None
        assert index.notes.get(note_id).path == "Backend/context.md"
        assert index.notes.get_parsed(note_id).path == "Backend/context.md"
        moved_chunk = index.chunks.list_for_note(note_id)[0]
        assert moved_chunk.chunk_id == original_chunk.chunk_id
        assert moved_chunk.embedding_text_hash == original_chunk.embedding_text_hash
        assert moved_chunk.metadata["path"] == "Backend/context.md"

        after = parse_markdown(raw, "Backend/context.md")
        assert index.index_note(after, chunk_note(after), note_id=note_id) == note_id
        reindexed_chunk = index.chunks.list_for_note(note_id)[0]
        assert reindexed_chunk.chunk_id == original_chunk.chunk_id
        assert reindexed_chunk.embedding_text_hash == original_chunk.embedding_text_hash


def test_rename_path_collision_rolls_back(tmp_path: Path) -> None:
    first = parse_markdown("# First\n\nOne.\n", "First.md")
    second = parse_markdown("# Second\n\nTwo.\n", "Second.md")
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        first_id = index.index_note(first, chunk_note(first))
        second_id = index.index_note(second, chunk_note(second))
        original_chunk = index.chunks.list_for_note(first_id)[0]
        with pytest.raises(sqlite3.IntegrityError):
            index.notes.update_path(first_id, "Second.md")
        assert index.notes.get(first_id).path == "First.md"
        assert index.notes.get(second_id).path == "Second.md"
        assert index.chunks.list_for_note(first_id)[0] == original_chunk


def test_target_link_reconciliation_and_delete_cascade(tmp_path: Path) -> None:
    source = parse_markdown("# Source\n\nSee [[Target]].\n", "Source.md")
    target = parse_markdown("# Target\n\nBody.\n", "Target.md")
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        source_id = index.index_note(source, chunk_note(source))
        assert index.links_for_note(source_id)[0].target_note_id is None
        target_id = index.index_note(target, chunk_note(target))
        assert index.links_for_note(source_id)[0].target_note_id == target_id

        index.notes.delete(target_id)
        assert index.links_for_note(source_id)[0].target_note_id is None
        index.notes.delete(source_id)
        for table in ("notes", "chunks", "tags", "links", "blocks"):
            assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_foreign_key_rejects_orphan_chunk(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                """INSERT INTO chunks (
                    id, note_id, heading_path, raw_content, embedding_text,
                    content_hash, embedding_text_hash, token_count, position, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("orphan", "missing", "[]", "x", "x", "a", "b", 1, 0, "{}"),
            )


def test_outer_transaction_rollback_undoes_nested_index_write(tmp_path: Path) -> None:
    note = sample_note()
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        with pytest.raises(RuntimeError):
            with db.transaction():
                index.index_note(note, chunk_note(note))
                raise RuntimeError("abort")
        assert index.notes.get_by_path(note.path) is None
        assert db.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_failed_update_rolls_back_note_and_chunks(tmp_path: Path) -> None:
    original = sample_note()
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        note_id = index.index_note(original, chunk_note(original))
        prior_chunks = index.chunks.list_for_note(note_id)
        changed = sample_note(extra="\nChanged.\n")
        invalid_chunks = chunk_note(changed)
        with pytest.raises(ValueError, match="contiguous"):
            index.index_note(changed, [invalid_chunks[0], invalid_chunks[0]], note_id=note_id)
        assert index.notes.get_parsed(note_id) == original
        assert index.chunks.list_for_note(note_id) == prior_chunks


def test_dirty_state_and_clear_rebuild(tmp_path: Path) -> None:
    note = sample_note()
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        index.mark_dirty(note.path, "created")
        assert index.list_dirty()[0].note_id is None
        index.set_state("last_scan", "done")
        assert index.get_state("last_scan") == "done"
        note_id = index.index_note(note, chunk_note(note))
        assert index.list_dirty() == []
        index.mark_dirty(note.path, "modified")
        assert index.list_dirty()[0].note_id == note_id
        index.clear()
        assert index.notes.get(note_id) is None
        assert index.list_dirty() == []
        assert index.get_state("last_scan") is None
        assert index.index_note(note, chunk_note(note)) != note_id


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(SchemaError, match="Unsupported"):
        Database(path)


def test_unversioned_nonempty_database_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.close()
    with pytest.raises(SchemaError, match="not empty"):
        Database(path)


def test_versioned_incomplete_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    with pytest.raises(SchemaError, match="incomplete"):
        Database(path)
