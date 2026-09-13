import os
from pathlib import Path

import pytest

from obsai.errors import ParseError, VaultError
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository


def write_note(vault: Path, relative: str, content: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_create_unchanged_rename_move_modify_delete_lifecycle(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    original = write_note(
        vault, "A.md", "# Stable Title\n\nOriginal body #topic. See [[Reference]].\n"
    )
    write_note(vault, "Reference.md", "# Reference\n\nSee [[A]].\n")

    with Database(tmp_path / "index.db") as database:
        repository = IndexRepository(database)
        updater = IncrementalIndexer(repository)
        first = updater.update(vault)
        assert first.count("created") == 2
        assert first.count("unchanged") == 0
        note_id = repository.notes.get_by_path("A.md").id
        original_chunk = repository.chunks.list_for_note(note_id)[0]
        reference_id = repository.notes.get_by_path("Reference.md").id
        assert repository.links_for_note(reference_id)[0].target_note_id == note_id

        second = updater.update(vault)
        assert second.count("unchanged") == 2
        assert second.count("created") == 0
        assert repository.chunks.list_for_note(note_id)[0] == original_chunk

        renamed = original.rename(vault / "B.md")
        rename_result = updater.update(vault)
        assert rename_result.count("renamed") == 1
        assert rename_result.count("moved") == 0
        assert rename_result.count("unchanged") == 1
        assert rename_result.affected_link_count == 1
        rename = next(change for change in rename_result.changes if change.kind == "renamed")
        assert rename.old_path == "A.md"
        assert rename.path == "B.md"
        assert rename.affected_links[0].source_path == "Reference.md"
        assert repository.notes.get_by_path("B.md").id == note_id
        assert repository.notes.get_by_path("A.md") is None
        renamed_chunk = repository.chunks.list_for_note(note_id)[0]
        assert renamed_chunk.chunk_id == original_chunk.chunk_id
        assert renamed_chunk.embedding_text_hash == original_chunk.embedding_text_hash

        moved = vault / "nested" / "B.md"
        moved.parent.mkdir()
        renamed.rename(moved)
        move_result = updater.update(vault)
        assert move_result.count("moved") == 1
        assert move_result.count("renamed") == 0
        assert repository.notes.get_by_path("nested/B.md").id == note_id
        assert repository.chunks.list_for_note(note_id)[0].chunk_id == original_chunk.chunk_id

        moved.write_text("# Stable Title\n\nModified body.\n", encoding="utf-8")
        modify_result = updater.update(vault)
        assert modify_result.count("modified") == 1
        assert modify_result.count("unchanged") == 1
        assert repository.notes.get_by_path("nested/B.md").id == note_id
        modified_chunk = repository.chunks.list_for_note(note_id)[0]
        assert modified_chunk.chunk_id != original_chunk.chunk_id
        assert "Modified body" in modified_chunk.raw_content

        moved.unlink()
        delete_result = updater.update(vault)
        assert delete_result.count("deleted") == 1
        assert repository.notes.get(note_id) is None
        assert repository.chunks.list_for_note(note_id) == []
        assert repository.tags_for_note(note_id) == []
        assert repository.links_for_note(note_id) == []
        assert repository.blocks_for_note(note_id) == []
        assert repository.links_for_note(reference_id)[0].target_note_id is None
        assert repository.get_state("last_update") is not None


def test_rename_and_modify_is_not_exact_rename(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    original = write_note(vault, "A.md", "# Title\n\nOld body.\n")
    with Database(tmp_path / "index.db") as database:
        repository = IndexRepository(database)
        updater = IncrementalIndexer(repository)
        updater.update(vault)
        old_id = repository.notes.get_by_path("A.md").id
        renamed = original.rename(vault / "B.md")
        renamed.write_text("# Title\n\nNew body.\n", encoding="utf-8")
        result = updater.update(vault)
        assert result.count("renamed") == 0
        assert result.count("moved") == 0
        assert result.count("created") == 1
        assert result.count("deleted") == 1
        assert repository.notes.get_by_path("B.md").id != old_id


def test_mtime_change_without_content_change_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obsai.indexing.incremental as incremental

    vault = tmp_path / "vault"
    vault.mkdir()
    path = write_note(vault, "A.md", "# Stable\n\nBody.\n")
    with Database(tmp_path / "index.db") as database:
        updater = IncrementalIndexer(IndexRepository(database))
        updater.update(vault)
        original_chunk_note = incremental.chunk_note

        def unexpected_rechunk(*args, **kwargs):
            raise AssertionError("unchanged file was re-chunked")

        monkeypatch.setattr(incremental, "chunk_note", unexpected_rechunk)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert updater.update(vault).count("unchanged") == 1
        monkeypatch.setattr(incremental, "chunk_note", original_chunk_note)


def test_content_change_with_restored_mtime_is_modified(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = write_note(vault, "A.md", "# Stable\n\nOld body.\n")
    with Database(tmp_path / "index.db") as database:
        updater = IncrementalIndexer(IndexRepository(database))
        updater.update(vault)
        stat = path.stat()
        path.write_text("# Stable\n\nNew body.\n", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert updater.update(vault).count("modified") == 1


def test_ambiguous_equal_content_is_not_guessed_as_rename(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    content = "# Same\n\nIdentical.\n"
    first = write_note(vault, "A.md", content)
    second = write_note(vault, "B.md", content)
    with Database(tmp_path / "index.db") as database:
        updater = IncrementalIndexer(IndexRepository(database))
        updater.update(vault)
        first.rename(vault / "C.md")
        second.rename(vault / "D.md")
        result = updater.update(vault)
        assert result.count("renamed") == 0
        assert result.count("created") == 2
        assert result.count("deleted") == 2


def test_parse_failure_does_not_partially_update_database(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = write_note(vault, "A.md", "# A\n\nOld.\n")
    second = write_note(vault, "B.md", "# B\n\nValid.\n")
    with Database(tmp_path / "index.db") as database:
        repository = IndexRepository(database)
        updater = IncrementalIndexer(repository)
        updater.update(vault)
        old_a = repository.notes.get_by_path("A.md")
        first.write_text("# A\n\nNew.\n", encoding="utf-8")
        second.write_text("---\nbroken: [\n", encoding="utf-8")
        with pytest.raises(ParseError):
            updater.update(vault)
        assert repository.notes.get_by_path("A.md") == old_a
        assert repository.notes.get_by_path("B.md") is not None


def test_rename_file_changing_during_update_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obsai.indexing.incremental as incremental

    vault = tmp_path / "vault"
    vault.mkdir()
    original = write_note(vault, "A.md", "# Stable\n\nBody.\n")
    with Database(tmp_path / "index.db") as database:
        repository = IndexRepository(database)
        updater = IncrementalIndexer(repository)
        updater.update(vault)
        note_id = repository.notes.get_by_path("A.md").id
        original.rename(vault / "B.md")
        read_note = incremental._read_note
        reads = 0

        def changing_read(path: Path) -> str:
            nonlocal reads
            content = read_note(path)
            if path.name == "B.md":
                reads += 1
                if reads == 2:
                    return content + "Concurrent edit.\n"
            return content

        monkeypatch.setattr(incremental, "_read_note", changing_read)
        with pytest.raises(VaultError, match="changed during"):
            updater.update(vault)
        assert repository.notes.get(note_id).path == "A.md"
        assert repository.notes.get_by_path("B.md") is None


def test_ignored_note_never_enters_incremental_index(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsaiignore").write_text("Private/\n", encoding="utf-8")
    write_note(vault, "Private/bad.md", "---\nbroken: [\n")
    write_note(vault, "Public.md", "# Public\n\nVisible.\n")
    with Database(tmp_path / "index.db") as database:
        repository = IndexRepository(database)
        result = IncrementalIndexer(repository).update(vault)
        assert result.count("created") == 1
        assert repository.notes.get_by_path("Private/bad.md") is None
