"""A-7 acceptance: the organizer split keeps proposals identical and gates apply.

``organize inbox`` used to interleave scanning, numbering, selection and
application. Splitting it is only safe if the read-only half produces exactly what
it produced before and the write half cannot be reached without an approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obsai.application import changes
from obsai.application.organizer import apply, plan_selection, propose
from obsai.config.models import Settings
from obsai.errors import ConflictError
from obsai.indexing import IncrementalIndexer
from obsai.organizer import InboxOrganizer
from obsai.retrieval import FTSRetriever
from obsai.storage import Database, IndexRepository


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def fixture(tmp_path: Path, *, second: bool = False):
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
    settings = Settings(vault={"path": vault}, index={"database": database_path})
    return vault, database_path, database, repository, settings


@pytest.fixture(autouse=True)
def clean_store():
    changes.reset()
    yield
    changes.reset()


def test_proposals_match_the_domain_organizer_one_for_one(tmp_path: Path) -> None:
    vault, database_path, database, repository, settings = fixture(tmp_path, second=True)
    write(vault, "Ref.md", "See [[Inbox/redis]] and [[Inbox/queue]]\n")

    scan = propose(vault, database_path, database, settings)
    domain = InboxOrganizer(vault, database_path, repository, FTSRetriever(database)).propose()

    assert len(scan.proposals) == len(domain)
    for index, (view, original) in enumerate(zip(scan.preview.proposals, domain), start=1):
        assert view.number == index
        assert view.path == original.path
        assert view.destination == original.destination
        assert view.title == original.title
        assert view.add_tags == tuple(original.add_tags)
        assert view.add_links == tuple(link.wikilink for link in original.add_links)
        assert view.affected_backlinks == tuple(original.affected_backlinks)
        assert view.confidence == original.confidence
        assert view.reason == original.reason
        assert view.issue == original.issue
        assert view.actionable == original.actionable
        assert view.selected_by_default == original.selected_by_default


def test_default_numbers_track_the_default_selection(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    expected = tuple(
        item.number for item in scan.preview.proposals if item.selected_by_default
    )
    assert scan.preview.default_numbers == expected


def test_proposing_writes_nothing(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    before = {path.name: path.read_bytes() for path in sorted(vault.rglob("*.md"))}
    scan = propose(vault, database_path, database, settings)
    assert scan.preview.proposals
    assert {path.name: path.read_bytes() for path in sorted(vault.rglob("*.md"))} == before


def test_apply_refuses_a_missing_nonce(tmp_path: Path) -> None:
    """The acceptance property: a plan id alone is not an approval."""
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])

    with pytest.raises(ConflictError, match="nonce"):
        apply(view, nonce="")
    with pytest.raises(ConflictError, match="nonce"):
        apply(view)
    assert (vault / "Inbox/redis.md").exists()
    assert not (vault / "Backend/Redis Cache.md").exists()


def test_apply_refuses_a_forged_nonce(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])

    with pytest.raises(ConflictError):
        apply(view, nonce="0" * 32)
    assert (vault / "Inbox/redis.md").exists()


def test_apply_refuses_an_unknown_plan(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])
    changes.discard(view)

    with pytest.raises(ConflictError):
        apply(view)
    assert (vault / "Inbox/redis.md").exists()


def test_a_declined_selection_changes_nothing(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])

    outcome = apply(view, approved=False)
    assert outcome.cancelled and not outcome.committed
    assert (vault / "Inbox/redis.md").exists()
    assert not (vault / "Backend/Redis Cache.md").exists()


def test_an_approved_selection_moves_the_note(tmp_path: Path) -> None:
    vault, database_path, database, repository, settings = fixture(tmp_path)
    note_id = repository.notes.get_by_path("Inbox/redis.md").id
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])

    assert view.batch is True
    assert view.changes[0].operation == "move"
    assert view.changes[0].destination == "Backend/Redis Cache.md"

    outcome = apply(view, nonce=view.nonce)
    assert outcome.committed
    assert not (vault / "Inbox/redis.md").exists()
    moved = vault / "Backend/Redis Cache.md"
    assert moved.exists()
    assert "redis" in moved.read_text()
    with Database(database_path) as reopened:
        assert IndexRepository(reopened).notes.get_by_path("Backend/Redis Cache.md").id == note_id


def test_the_diff_is_carried_for_the_review_step(tmp_path: Path) -> None:
    vault, database_path, database, _, settings = fixture(tmp_path)
    scan = propose(vault, database_path, database, settings)
    view = plan_selection(scan, [1])
    assert view.diff, "a reviewer needs the diff before approving"
    assert {line.style for line in view.diff} <= {"added", "removed", "hunk", "notice", None}
