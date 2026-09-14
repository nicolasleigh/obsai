import os
import io
from pathlib import Path
from uuid import uuid4

import pytest
from rich.console import Console

from obsai.errors import CollisionError, ConflictError, RecoveryRequiredError, TransactionError
from obsai.safe_write.models import ChangeSet
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionOperation as Op
from obsai.transactions import TransactionService
from obsai.transactions.journal import TransactionJournal


def test_multi_file_commit_and_cancel(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_bytes(b"A old")
    (tmp_path / "B.md").write_bytes(b"B old")
    indexed = []
    service = TransactionService(tmp_path, indexer=lambda root: indexed.append(root))
    plan = service.plan([Op.replace("A.md", "old", "new"), Op.frontmatter("B.md", {"status": "done"})])
    cancelled = service.execute(plan, approved=False)
    assert cancelled.cancelled and not cancelled.committed
    assert (tmp_path / "A.md").read_bytes() == b"A old"
    assert not (tmp_path / ".obsai-transactions").exists()
    committed = service.execute(plan, approved=True)
    assert committed.committed and not committed.index_dirty
    assert (tmp_path / "A.md").read_bytes() == b"A new"
    assert b"status: done" in (tmp_path / "B.md").read_bytes()
    assert indexed == [tmp_path.resolve()]
    assert not TransactionService.journals(tmp_path)


def test_second_operation_failure_rolls_back_first(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "A.md").write_bytes(b"A old")
    (tmp_path / "B.md").write_bytes(b"B old")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.replace("A.md", "old", "new"), Op.replace("B.md", "old", "new")])
    original_apply = service.safe.apply
    calls = 0

    def fail_second(change, *, approved):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second operation failure")
        return original_apply(change, approved=approved)

    monkeypatch.setattr(service.safe, "apply", fail_second)
    with pytest.raises(TransactionError, match="rolled back"):
        service.execute(plan, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"A old"
    assert (tmp_path / "B.md").read_bytes() == b"B old"
    assert not TransactionService.journals(tmp_path)


def test_hash_conflict_and_destination_collision_fail_before_journal(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_bytes(b"old")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.move("A.md", "B.md")])
    (tmp_path / "A.md").write_bytes(b"new")
    with pytest.raises(ConflictError):
        service.execute(plan, approved=True)
    assert not (tmp_path / ".obsai-transactions").exists()
    (tmp_path / "A.md").write_bytes(b"old")
    (tmp_path / "B.md").write_bytes(b"collision")
    with pytest.raises(CollisionError):
        service.execute(plan, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"old"
    assert (tmp_path / "B.md").read_bytes() == b"collision"


def test_permission_preflight_fails_before_snapshot(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "A.md").write_bytes(b"old")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.replace("A.md", "old", "new")])
    original_access = os.access

    def deny(path, mode):
        if Path(path) == tmp_path and mode & os.W_OK:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", deny)
    with pytest.raises(TransactionError, match="not writable"):
        service.execute(plan, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"old"
    assert not (tmp_path / ".obsai-transactions").exists()


def test_index_failure_keeps_vault_commit_and_marks_dirty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_bytes(b"old")
    database_path = tmp_path / "index.db"
    with Database(database_path):
        pass

    def fail_index(root):
        raise OSError("injected index failure")

    service = TransactionService(vault, database_path=database_path, indexer=fail_index)
    plan = service.plan([Op.replace("A.md", "old", "new")])
    result = service.execute(plan, approved=True)
    assert result.committed and result.index_dirty
    assert (vault / "A.md").read_bytes() == b"new"
    journal = TransactionService.journals(vault)[0]
    assert journal["status"] == "index_dirty"
    assert journal["dirty_paths"] == ["A.md"]
    with Database(database_path) as database:
        assert [item.path for item in IndexRepository(database).list_dirty()] == ["A.md"]
    service.clear_index_dirty()
    assert not TransactionService.journals(vault)


def test_index_failure_after_id_migration_rolls_back_db_only(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_bytes(b"# A\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        repository = IndexRepository(database)
        IncrementalIndexer(repository).update(vault)
        note_id = repository.notes.get_by_path("A.md").id

    def fail_update(self, root):
        raise OSError("injected failure after path migration")

    monkeypatch.setattr(IncrementalIndexer, "update", fail_update)
    service = TransactionService(vault, database_path=database_path)
    plan = service.plan([Op.move("A.md", "B.md")])
    result = service.execute(plan, approved=True)
    assert result.committed and result.index_dirty
    assert (vault / "B.md").read_bytes() == b"# A\n"
    assert not (vault / "A.md").exists()
    with Database(database_path) as database:
        repository = IndexRepository(database)
        assert repository.notes.get_by_path("A.md").id == note_id
        assert repository.notes.get_by_path("B.md") is None
        assert {item.path for item in repository.list_dirty()} == {"A.md", "B.md"}


def test_crash_window_is_detected_and_recoverable(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_bytes(b"old")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.replace("A.md", "old", "new")])
    transaction_id = uuid4().hex
    journal = TransactionJournal.create(service.root, transaction_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)
    # Simulate a crash before the applied_count write.
    assert TransactionService.journals(tmp_path)[0]["status"] == "applying"
    with pytest.raises(RecoveryRequiredError):
        service.plan([Op.create("B.md", "new")])
    cancelled = service.recover(transaction_id, approved=False)
    assert cancelled.cancelled
    assert (tmp_path / "A.md").read_bytes() == b"new"
    service.recover(transaction_id, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"old"
    assert not TransactionService.journals(tmp_path)


def test_recovery_restores_a_partially_applied_move(tmp_path: Path) -> None:
    (tmp_path / "A.md").write_bytes(b"original")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.move("A.md", "Nested/B.md"), Op.frontmatter("Nested/B.md", {"x": 1})])
    transaction_id = uuid4().hex
    journal = TransactionJournal.create(service.root, transaction_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)
    service.recover(transaction_id, approved=True)
    assert (tmp_path / "A.md").read_bytes() == b"original"
    assert not (tmp_path / "Nested" / "B.md").exists()
    assert not (tmp_path / "Nested").exists()


def test_orphan_snapshot_directory_is_reported(tmp_path: Path) -> None:
    orphan = tmp_path / ".obsai-transactions" / uuid4().hex
    orphan.mkdir(parents=True)
    with pytest.raises(RecoveryRequiredError, match="missing or unsafe"):
        TransactionService.journals(tmp_path)


def test_recovery_refuses_unknown_external_file_state(tmp_path: Path) -> None:
    source = tmp_path / "A.md"
    source.write_bytes(b"old")
    service = TransactionService(tmp_path)
    plan = service.plan([Op.replace("A.md", "old", "new")])
    transaction_id = uuid4().hex
    TransactionJournal.create(service.root, transaction_id, plan).update(status="applying")
    source.write_bytes(b"unrelated external change")
    with pytest.raises(RecoveryRequiredError, match="known state"):
        service.preview_recovery(transaction_id, Console(file=io.StringIO()))
    assert source.read_bytes() == b"unrelated external change"


def test_move_rewrites_explicit_links_and_skips_ambiguous_or_code(tmp_path: Path) -> None:
    (tmp_path / "Go").mkdir()
    (tmp_path / "Go" / "context.md").write_bytes(b"# Context\n")
    reference = tmp_path / "Refs.md"
    reference.write_text(
        "See [[Go/context#Heading|Alias]] and ![[Go/context.md#^block]].\n"
        "Ambiguous [[context]] and [[context.md]] stay.\n"
        "Mixed [[Go/context]] and `[[Go/context]]` stay together.\n"
        "```md\n[[Go/context]]\n```\n",
        encoding="utf-8",
    )
    service = TransactionService(tmp_path)
    plan = service.plan_move_with_backlinks("Go/context.md", "Archive/context.md")
    assert len(plan.changes) == 2
    assert plan.ambiguous_backlinks == ("Refs.md",)
    result = service.execute(plan, approved=True)
    assert result.committed
    assert not (tmp_path / "Go" / "context.md").exists()
    assert (tmp_path / "Archive" / "context.md").read_bytes() == b"# Context\n"
    content = reference.read_text(encoding="utf-8")
    assert "[[Archive/context#Heading|Alias]]" in content
    assert "![[Archive/context.md#^block]]" in content
    assert "[[context]]" in content
    assert "[[context.md]]" in content
    assert "Mixed [[Go/context]] and `[[Go/context]]`" in content
    assert "```md\n[[Go/context]]\n```" in content


def test_move_then_frontmatter_update_preserves_index_note_id(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_bytes(b"# Stable\n\nBody.\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        repository = IndexRepository(database)
        IncrementalIndexer(repository).update(vault)
        note_id = repository.notes.get_by_path("A.md").id
    service = TransactionService(vault, database_path=database_path)
    plan = service.plan([
        Op.move("A.md", "Archive/B.md"),
        Op.frontmatter("Archive/B.md", {"status": "done"}),
    ])
    result = service.execute(plan, approved=True)
    assert result.committed and not result.index_dirty
    assert b"status: done" in (vault / "Archive" / "B.md").read_bytes()
    with Database(database_path) as database:
        repository = IndexRepository(database)
        assert repository.notes.get_by_path("A.md") is None
        assert repository.notes.get_by_path("Archive/B.md").id == note_id


def test_backlink_rewrite_failure_during_apply_rolls_back_move(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "Go").mkdir()
    source = tmp_path / "Go" / "context.md"
    source.write_bytes(b"# Context\n")
    source.chmod(0o640)
    reference = tmp_path / "Refs.md"
    reference.write_bytes(b"[[Go/context]]\n")
    service = TransactionService(tmp_path)
    plan = service.plan_move_with_backlinks("Go/context.md", "Archive/context.md")
    original_apply = service.safe.apply

    def fail_backlink(change, *, approved):
        if change.file.path == "Refs.md":
            raise OSError("injected backlink rewrite failure")
        return original_apply(change, approved=approved)

    monkeypatch.setattr(service.safe, "apply", fail_backlink)
    with pytest.raises(TransactionError, match="rolled back"):
        service.execute(plan, approved=True)
    assert source.read_bytes() == b"# Context\n"
    assert source.stat().st_mode & 0o777 == 0o640
    assert reference.read_bytes() == b"[[Go/context]]\n"
    assert not (tmp_path / "Archive" / "context.md").exists()
    assert not (tmp_path / "Archive").exists()
