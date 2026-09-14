import io
import os
from pathlib import Path

import pytest
from rich.console import Console

from obsai.errors import CollisionError, ConflictError, InvalidEncodingError, SafeWriteError
from obsai.safe_write import SafeWriteService


def test_basic_update_preview_and_approval(tmp_path: Path) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"# A\n\nOld text.\n")
    service = SafeWriteService(tmp_path)
    change = service.update_note("A.md", "Old", "New")
    output = io.StringIO()
    service.preview(change, Console(file=output, color_system="standard", force_terminal=True))
    assert "-Old text." in output.getvalue()
    assert "+New text." in output.getvalue()
    assert note.read_bytes() == b"# A\n\nOld text.\n"
    assert service.apply(change, approved=True)
    assert note.read_bytes() == b"# A\n\nNew text.\n"


def test_conflict_and_disappeared_note(tmp_path: Path) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"before")
    service = SafeWriteService(tmp_path)
    change = service.update_note("A.md", "before", "after")
    note.write_bytes(b"external edit")
    with pytest.raises(ConflictError):
        service.apply(change, approved=True)
    assert note.read_bytes() == b"external edit"
    note.write_bytes(b"\xff")
    with pytest.raises(ConflictError, match="changed"):
        service.apply(change, approved=True)
    note.unlink()
    with pytest.raises(ConflictError, match="disappeared"):
        service.apply(change, approved=True)
    assert not note.exists()


def test_create_and_move_collision(tmp_path: Path) -> None:
    service = SafeWriteService(tmp_path)
    created = service.create_note("A.md", "new")
    (tmp_path / "A.md").write_bytes(b"racing create")
    with pytest.raises(CollisionError):
        service.apply(created, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"racing create"
    (tmp_path / "B.md").write_bytes(b"occupied")
    with pytest.raises(CollisionError):
        service.move_note("A.md", "B.md")


@pytest.mark.parametrize("path", ["../outside.md", "nested/../../outside.md", "/tmp/outside.md", "sub\\..\\outside.md"])
def test_path_traversal_rejected(tmp_path: Path, path: str) -> None:
    service = SafeWriteService(tmp_path)
    with pytest.raises(SafeWriteError):
        service.create_note(path, "bad")


def test_symlink_outside_vault_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafeWriteError):
        SafeWriteService(tmp_path).create_note("link/x.md", "bad")
    assert not (outside / "x.md").exists()


def test_symlink_inserted_after_preview_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-external"
    outside.mkdir()
    service = SafeWriteService(tmp_path)
    change = service.create_note("nested/A.md", "safe")
    (tmp_path / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SafeWriteError, match="Symlinked"):
        service.apply(change, approved=True)
    assert not (outside / "A.md").exists()


def test_atomic_write_uses_same_directory_fsync_replace_and_preserves_mode(tmp_path: Path, monkeypatch) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"before")
    note.chmod(0o640)
    service = SafeWriteService(tmp_path)
    change = service.update_note("A.md", "before", "after")
    calls = []
    fsync_calls = []
    original_replace = os.replace
    original_fsync = os.fsync

    def observed_fsync(descriptor):
        fsync_calls.append(descriptor)
        return original_fsync(descriptor)

    def observed_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        calls.append((source, destination, source.read_bytes()))
        assert source.parent == destination.parent == tmp_path
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observed_replace)
    monkeypatch.setattr(os, "fsync", observed_fsync)
    assert service.apply(change, approved=True)
    assert len(calls) == 1
    assert len(fsync_calls) >= 1
    assert calls[0][2] == b"after"
    assert note.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".obsai-*.tmp"))


def test_failed_atomic_replace_keeps_original_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"before")
    service = SafeWriteService(tmp_path)
    change = service.update_note("A.md", "before", "after")

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SafeWriteError, match="injected replace failure"):
        service.apply(change, approved=True)
    assert note.read_bytes() == b"before"
    assert not list(tmp_path.glob(".obsai-*.tmp"))


def test_cancelled_approval_is_byte_for_byte_unchanged(tmp_path: Path) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"\xef\xbb\xbfOriginal\r\n")
    service = SafeWriteService(tmp_path)
    change = service.update_note("A.md", "Original", "Changed")
    before = note.read_bytes()
    assert not service.apply(change, approved=False)
    assert note.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["A.md"]


def test_trash_moves_to_recoverable_hidden_directory(tmp_path: Path) -> None:
    nested = tmp_path / "Notes"
    nested.mkdir()
    note = nested / "A.md"
    note.write_bytes(b"# A\n")
    service = SafeWriteService(tmp_path)
    change = service.trash_note("Notes/A.md")
    assert change.file.destination.startswith(".obsai-trash/")
    assert service.apply(change, approved=True)
    assert not note.exists()
    assert (tmp_path / change.file.destination).read_bytes() == b"# A\n"


def test_invalid_encoding_rejected(tmp_path: Path) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"\xff")
    service = SafeWriteService(tmp_path)
    with pytest.raises(InvalidEncodingError):
        service.update_note("A.md", "x", "y")
    with pytest.raises(InvalidEncodingError):
        service.create_note("B.md", "\ud800")


def test_frontmatter_patch_preserves_body(tmp_path: Path) -> None:
    note = tmp_path / "A.md"
    note.write_bytes(b"---\nstatus: todo\n---\n# A\n\nBody.\n")
    service = SafeWriteService(tmp_path)
    change = service.update_frontmatter("A.md", {"status": "done", "rating": 8})
    assert change.file.new_content.endswith("# A\n\nBody.\n")
    assert service.apply(change, approved=True)
    assert b"status: done\nrating: 8" in note.read_bytes()


def test_move_reports_backlink_impact_without_rewriting_it(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_bytes(b"# A\n")
    (tmp_path / "Reference.md").write_bytes(b"See [[A]].\n")
    service = SafeWriteService(tmp_path)
    change = service.move_note("A.md", "Folder/B.md")
    assert change.file.affected_backlinks == ("Reference.md",)
    output = io.StringIO()
    service.preview(change, Console(file=output))
    assert "Reference.md" in output.getvalue()
    assert service.apply(change, approved=True)
    assert (tmp_path / "Folder" / "B.md").read_bytes() == b"# A\n"
    assert (tmp_path / "Reference.md").read_bytes() == b"See [[A]].\n"


def test_move_collision_after_preview_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "A.md"
    source.write_bytes(b"source")
    service = SafeWriteService(tmp_path)
    change = service.move_note("A.md", "B.md")
    (tmp_path / "B.md").write_bytes(b"racing destination")
    with pytest.raises(CollisionError):
        service.apply(change, approved=True)
    assert source.read_bytes() == b"source"
    assert (tmp_path / "B.md").read_bytes() == b"racing destination"


def test_move_checks_source_hash_at_commit(tmp_path: Path) -> None:
    source = tmp_path / "A.md"
    source.write_bytes(b"source")
    service = SafeWriteService(tmp_path)
    change = service.move_note("A.md", "B.md")
    source.write_bytes(b"changed")
    with pytest.raises(ConflictError):
        service.apply(change, approved=True)
    assert source.read_bytes() == b"changed"
    assert not (tmp_path / "B.md").exists()
