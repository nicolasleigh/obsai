from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from obsai.cli.app import app
from obsai.safe_write.models import ChangeSet
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionOperation as Op
from obsai.transactions import TransactionService
from obsai.transactions.journal import TransactionJournal


def _configure(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    database_path = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )
    return vault, database_path


def test_cli_move_rewrites_only_explicit_links_and_preserves_note_id(tmp_path: Path) -> None:
    vault, database_path = _configure(tmp_path)
    (vault / "Go").mkdir()
    (vault / "Go" / "context.md").write_bytes(b"# Context\n")
    reference = vault / "Ref.md"
    reference.write_bytes(b"[[Go/context|Alias]] and [[context]]\n")
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    with Database(database_path) as database:
        note_id = IndexRepository(database).notes.get_by_path("Go/context.md").id
    declined = runner.invoke(
        app, ["note", "move", "Go/context.md", "Archive/context.md"], input="n\n"
    )
    assert declined.exit_code == 0, declined.output
    assert "Cancelled" in declined.output
    assert (vault / "Go" / "context.md").read_bytes() == b"# Context\n"
    assert reference.read_bytes() == b"[[Go/context|Alias]] and [[context]]\n"
    assert not TransactionService.journals(vault)
    moved = runner.invoke(
        app, ["note", "move", "Go/context.md", "Archive/context.md"], input="y\n"
    )
    assert moved.exit_code == 0, moved.output
    assert "Affected backlinks" in moved.output
    assert "Ambiguous WikiLinks left unchanged" in moved.output
    assert reference.read_bytes() == b"[[Archive/context|Alias]] and [[context]]\n"
    with Database(database_path) as database:
        assert IndexRepository(database).notes.get_by_path("Archive/context.md").id == note_id


def test_cli_detects_and_guides_recovery(tmp_path: Path) -> None:
    vault, _ = _configure(tmp_path)
    source = vault / "A.md"
    source.write_bytes(b"old")
    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "old", "new")])
    transaction_id = uuid4().hex
    journal = TransactionJournal.create(service.root, transaction_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    runner = CliRunner()
    status = runner.invoke(app, ["transaction", "status"])
    assert status.exit_code == 0, status.output
    assert transaction_id in status.output
    assert "Recovery required" in status.output
    blocked = runner.invoke(app, ["note", "create", "B.md", "--content", "new"], input="y\n")
    assert blocked.exit_code != 0
    assert not (vault / "B.md").exists()
    cancelled = runner.invoke(app, ["transaction", "recover", transaction_id], input="n\n")
    assert cancelled.exit_code == 0
    assert source.read_bytes() == b"new"
    recovered = runner.invoke(app, ["transaction", "recover", transaction_id], input="y\n")
    assert recovered.exit_code == 0, recovered.output
    assert "-new" in recovered.output and "+old" in recovered.output
    assert "Recovered" in recovered.output
    assert source.read_bytes() == b"old"
    assert not TransactionService.journals(vault)


def test_index_update_clears_committed_dirty_journal(tmp_path: Path) -> None:
    vault, database_path = _configure(tmp_path)
    (vault / "A.md").write_bytes(b"# A\n\nOld.\n")
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0

    def fail_index(root):
        raise OSError("injected index failure")

    service = TransactionService(vault, database_path=database_path, indexer=fail_index)
    plan = service.plan([Op.replace("A.md", "Old", "New")])
    assert service.execute(plan, approved=True).index_dirty
    assert TransactionService.journals(vault)[0]["status"] == "index_dirty"
    updated = runner.invoke(app, ["index", "update"])
    assert updated.exit_code == 0, updated.output
    assert "Index recovery required" in updated.output
    assert not TransactionService.journals(vault)
    with Database(database_path) as database:
        repository = IndexRepository(database)
        assert repository.notes.get_by_path("A.md") is not None
        assert repository.list_dirty() == []
