from pathlib import Path

import pytest
from typer.testing import CliRunner

from obsai.cli.app import app
from obsai.errors import ConflictError, TransactionError
from obsai.indexing import IncrementalIndexer
from obsai.organizer import InboxOrganizer
from obsai.retrieval import FTSRetriever
from obsai.storage import Database, IndexRepository


def write(vault: Path, path: str, content: str):
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def setup(tmp_path, *, second=False):
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, "Backend/Redis.md", "---\ntags: [redis, backend]\n---\n# Redis Cache\n\nRedis cache strategy.\n")
    write(vault, "Inbox/redis.md", "# Redis Cache\n\nRedis cache notes.\n")
    if second:
        write(vault, "Backend/Queue.md", "---\ntags: [queue, backend]\n---\n# Message Queue\n\nQueue delivery.\n")
        write(vault, "Inbox/queue.md", "# Message Queue\n\nQueue delivery notes.\n")
    database_path = tmp_path / "index.db"
    database = Database(database_path)
    repository = IndexRepository(database)
    IncrementalIndexer(repository).update(vault)
    organizer = InboxOrganizer(vault, database_path, repository, FTSRetriever(database))
    return vault, database, organizer


def test_single_note_proposal_and_apply(tmp_path):
    vault, database, organizer = setup(tmp_path)
    note_id = IndexRepository(database).notes.get_by_path("Inbox/redis.md").id
    write(vault, "Ref.md", "See [[Inbox/redis]]\n")
    original = (vault / "Inbox/redis.md").read_bytes()
    proposals = organizer.propose()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.destination == "Backend/Redis Cache.md"
    assert proposal.title == "Redis Cache"
    assert set(proposal.add_tags) == {"redis", "backend"}
    assert proposal.add_links[0].wikilink == "[[Backend/Redis|Redis Cache]]"
    assert proposal.affected_backlinks == ("Ref.md",)
    assert proposal.selected_by_default
    assert (vault / "Inbox/redis.md").read_bytes() == original
    plan = organizer.plan(proposals, [1])
    assert (vault / "Inbox/redis.md").exists()
    result = organizer.apply(plan)
    assert result.committed
    assert not (vault / "Inbox/redis.md").exists()
    content = (vault / "Backend/Redis Cache.md").read_text()
    assert "tags:" in content and "redis" in content and "backend" in content
    assert "[[Backend/Redis|Redis Cache]]" in content
    assert "[[Backend/Redis Cache]]" in (vault / "Ref.md").read_text()
    assert IndexRepository(database).notes.get_by_path("Backend/Redis Cache.md").id == note_id
    database.close()


def test_multi_note_one_transaction_and_selected_subset(tmp_path):
    vault, database, organizer = setup(tmp_path, second=True)
    write(vault, "Ref.md", "[[Inbox/redis]] and [[Inbox/queue]]\n")
    proposals = organizer.propose()
    assert len(proposals) == 2
    plan = organizer.plan(proposals, [1, 2])
    assert len([change for change in plan.changes if change.operation == "move"]) == 2
    assert len([change for change in plan.changes if change.path == "Ref.md"]) == 1
    assert organizer.apply(plan).committed
    assert not list((vault / "Inbox").glob("*.md"))
    ref = (vault / "Ref.md").read_text()
    assert "[[Backend/Redis Cache]]" in ref
    assert "[[Backend/Message Queue]]" in ref
    database.close()


def test_unknown_destination_and_duplicate_destination_stay_unselected(tmp_path):
    vault, database, organizer = setup(tmp_path)
    write(vault, "Inbox/unknown.md", "# Unrelated Quantum Topic\n\nNo other note covers this.\n")
    write(vault, "Inbox/nested/redis.md", "# Redis Cache\n\nAnother Redis cache note.\n")
    proposals = organizer.propose()
    assert len(proposals) == 3
    unknown = next(item for item in proposals if item.path == "Inbox/unknown.md")
    assert unknown.destination is None and not unknown.selected_by_default
    duplicates = [item for item in proposals if item.destination == "Backend/Redis Cache.md"]
    assert len(duplicates) == 2
    assert all(item.issue and not item.selected_by_default for item in duplicates)
    with pytest.raises(TransactionError):
        organizer.plan(proposals, [proposals.index(unknown) + 1])
    database.close()


def test_low_confidence_existing_directory_is_not_default_selected(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, "Backend/Unique Guide.md", "# Unique Guide\n\nUnique.\n")
    write(vault, "Inbox/unique.md", "# Unique\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        repository = IndexRepository(database)
        IncrementalIndexer(repository).update(vault)
        organizer = InboxOrganizer(vault, database_path, repository, FTSRetriever(database))
        proposal = organizer.propose()[0]
        assert organizer.plan([proposal], [1]).changes
    assert proposal.destination == "Backend/Unique.md"
    assert proposal.issue is None
    assert proposal.confidence < 0.70
    assert not proposal.selected_by_default


def test_existing_destination_and_hash_conflict(tmp_path):
    vault, database, organizer = setup(tmp_path)
    proposals = organizer.propose()
    plan = organizer.plan(proposals, [1])
    (vault / "Inbox/redis.md").write_text("external edit")
    with pytest.raises(ConflictError):
        organizer.apply(plan)
    assert not (vault / "Backend/Redis Cache.md").exists()
    (vault / "Inbox/redis.md").write_text("# Redis Cache\n\nRedis cache notes.\n")
    write(vault, "Backend/Redis Cache.md", "occupied")
    proposals = organizer.propose()
    assert proposals[0].issue == "Destination already exists"
    assert not proposals[0].selected_by_default
    database.close()


def test_partial_transaction_failure_rolls_back(tmp_path, monkeypatch):
    vault, database, organizer = setup(tmp_path, second=True)
    proposals = organizer.propose()
    plan = organizer.plan(proposals, [1, 2])
    original = {path: (vault / path).read_bytes() for path in ("Inbox/redis.md", "Inbox/queue.md")}
    original_apply = organizer.transaction.safe.apply
    calls = 0

    def fail_second(change, *, approved):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected failure")
        return original_apply(change, approved=approved)

    monkeypatch.setattr(organizer.transaction.safe, "apply", fail_second)
    with pytest.raises(TransactionError, match="rolled back"):
        organizer.apply(plan)
    assert all((vault / path).read_bytes() == content for path, content in original.items())
    assert not (vault / "Backend/Redis Cache.md").exists()
    assert not (vault / "Backend/Message Queue.md").exists()
    database.close()


def test_cli_cancel_and_select_subset(tmp_path):
    vault, database, _ = setup(tmp_path, second=True)
    database.close()
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{tmp_path / "index.db"}"\n')
    runner = CliRunner()
    cancelled = runner.invoke(app, ["organize", "inbox"], input="q\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert "Inbox proposals" in cancelled.output
    assert "--- " not in cancelled.output
    assert (vault / "Inbox/redis.md").exists()
    viewed = runner.invoke(app, ["organize", "inbox"], input="v\n1\nq\n")
    assert viewed.exit_code == 0, viewed.output
    assert "--- " in viewed.output and "+++ " in viewed.output
    assert (vault / "Inbox/redis.md").exists()
    selected = runner.invoke(app, ["organize", "inbox"], input="s\n1\ny\n")
    assert selected.exit_code == 0, selected.output
    assert "Applied and verified 1 note" in selected.output
    assert not (vault / "Inbox/queue.md").exists() or not (vault / "Inbox/redis.md").exists()
    assert (vault / "Inbox/queue.md").exists() or (vault / "Inbox/redis.md").exists()


def test_cli_uses_configured_inbox(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, "Capture/draft.md", "# Redis Cache\n")
    write(vault, "Backend/Redis.md", "# Redis Cache\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n'
        '[organize]\ninbox = "Capture"\n'
    )
    result = CliRunner().invoke(app, ["organize", "inbox"], input="q\n")
    assert result.exit_code == 0, result.output
    assert "Capture/draft.md" in result.output
    assert (vault / "Capture/draft.md").exists()
