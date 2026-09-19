"""C-3a acceptance: the two index jobs refuse early, report honestly, and cancel safely.

Neither of the two properties the page promises is implemented by this module, and
that is why they are tested here: the tests pin behaviour that already exists so a
later change to the indexer cannot quietly take it away.

**A cancelled update keeps the old index.** ``IncrementalIndexer.update`` applies
every derived change inside one SQLite transaction, so a cancellation raised at any
checkpoint inside it rolls the whole update back. The job's own contribution is
that it reports ``cancelled`` rather than ``failed`` — which it gets for free by
letting ``JobCancelled``, a ``BaseException``, pass through.

**An interrupted rebuild keeps the live index.** ``ShadowIndexRebuilder`` builds and
validates a shadow database, compares the live file's fingerprint, and then swaps
with a single ``os.replace`` inside a deferred-signal region. A cancel on either
side of that one call leaves a complete index — the previous one, or the validated
new one — and never a hybrid.

The rest of the file covers the decisions that are this module's own: configuration
resolved while the request is still open, a step vocabulary rather than prose, and a
count that tells a user whether to wait two seconds or two minutes.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest

from obsai.application.index_jobs import (
    EMBEDDINGS_STALE,
    INDEX_REBUILD_KIND,
    INDEX_UPDATE_KIND,
    STEP_DONE,
    index_rebuild_job,
    index_update_job,
)
from obsai.application.jobs import JobRunner
from obsai.application.locks import vault_lock
from obsai.application.paths import (
    MISSING_VAULT_DIRECTORY_MESSAGE,
    NO_VAULT_MESSAGE,
    require_existing_vault,
)
from obsai.config.models import Settings
from obsai.errors import ConfigError, RecoveryRequiredError
from obsai.indexing import IncrementalIndexer
from obsai.shutdown import current_controller
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionOperation as Op
from obsai.transactions import TransactionService
from obsai.transactions.journal import TransactionJournal


@pytest.fixture()
def runner():
    with JobRunner() as instance:
        yield instance


def wait_for(runner: JobRunner, job_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = runner.get(job_id)
        if view.terminal:
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {runner.get(job_id)}")


def note(root: Path, name: str) -> None:
    (root / name).write_text(f"# {name}\n\n{name} body.\n", encoding="utf-8")


def make_vault(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    for name in names:
        note(root, name)
    return root


def build_index(vault: Path, index_path: Path) -> None:
    with Database(index_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)


def indexed_paths(index_path: Path) -> set[str]:
    """What the index on disk claims about the Vault, read from a fresh connection."""
    with Database(index_path) as database:
        return {record.path for record in IndexRepository(database).notes.list_index_facts()}


def settings_for(vault: Path, index_path: Path) -> Settings:
    return Settings(vault={"path": vault}, index={"database": index_path})


def freeze_for_recovery(vault: Path) -> None:
    """Leave an unfinished transaction behind without touching a single file."""
    service = TransactionService(vault)
    plan = service.plan([Op.create("Later.md", "content")])
    TransactionJournal.create(service.root, uuid4().hex, plan).update(status="applying")


# --------------------------------------------------------------------------- #
# Resolved while the request is still open
# --------------------------------------------------------------------------- #


def test_a_missing_vault_directory_is_refused_before_anything_is_queued(tmp_path: Path) -> None:
    """The job is built before it is submitted, so the failure is the request's."""
    absent = tmp_path / "absent"
    with pytest.raises(ConfigError) as raised:
        index_update_job(settings_for(absent, tmp_path / "index.db"))
    assert str(raised.value) == MISSING_VAULT_DIRECTORY_MESSAGE.format(path=absent)


def test_an_unconfigured_vault_uses_the_terminal_wording(tmp_path: Path) -> None:
    """One sentence for "no Vault", whether it is read from a terminal or a page."""
    settings = Settings(index={"database": tmp_path / "index.db"})
    for factory in (index_update_job, index_rebuild_job):
        with pytest.raises(ConfigError) as raised:
            factory(settings)
        assert str(raised.value) == NO_VAULT_MESSAGE


def test_a_symlinked_vault_root_is_accepted(tmp_path: Path) -> None:
    """A linked Vault is a Vault; the scanner resolves it, so the check does too."""
    real = make_vault(tmp_path, "A.md")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    settings = settings_for(link, tmp_path / "index.db")
    assert require_existing_vault(settings) == link
    assert callable(index_update_job(settings))


def test_an_unfinished_transaction_is_refused_before_anything_is_queued(tmp_path: Path) -> None:
    """423 rather than a queued job that fails a second later for the same reason."""
    vault = make_vault(tmp_path, "A.md")
    freeze_for_recovery(vault)
    settings = settings_for(vault, tmp_path / "index.db")
    for factory in (index_update_job, index_rebuild_job):
        with pytest.raises(RecoveryRequiredError):
            factory(settings)


# --------------------------------------------------------------------------- #
# What a finished job reports
# --------------------------------------------------------------------------- #


def test_the_update_job_indexes_the_vault_and_reports_the_counts(
    tmp_path: Path, runner: JobRunner
) -> None:
    vault = make_vault(tmp_path, "A.md", "B.md", "C.md")
    index_path = tmp_path / "index.db"
    settings = settings_for(vault, index_path)

    view = wait_for(runner, runner.submit(INDEX_UPDATE_KIND, index_update_job(settings)).job_id)

    assert view.status == "succeeded", view.error
    assert view.message == STEP_DONE
    assert view.detail["notes"] == 3
    assert view.detail["created"] == 3
    assert view.detail["unchanged"] == 0
    assert view.detail["affected_links"] == 0
    assert indexed_paths(index_path) == {"A.md", "B.md", "C.md"}


def test_the_update_job_counts_the_vault_rather_than_the_deletions(
    tmp_path: Path, runner: JobRunner
) -> None:
    """``len(changes)`` also counts rows for notes that are gone; the page must not."""
    vault = make_vault(tmp_path, "A.md", "B.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    (vault / "B.md").unlink()

    view = wait_for(
        runner,
        runner.submit(INDEX_UPDATE_KIND, index_update_job(settings_for(vault, index_path))).job_id,
    )

    assert view.status == "succeeded", view.error
    assert view.detail["notes"] == 1
    assert view.detail["deleted"] == 1
    assert view.detail["unchanged"] == 1
    assert indexed_paths(index_path) == {"A.md"}


def test_the_rebuild_job_reports_that_the_embeddings_are_gone(
    tmp_path: Path, runner: JobRunner
) -> None:
    """A fresh database has no vectors, and the page has to say so unprompted."""
    vault = make_vault(tmp_path, "A.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    note(vault, "B.md")

    view = wait_for(
        runner, runner.submit(INDEX_REBUILD_KIND, index_rebuild_job(settings_for(vault, index_path))).job_id
    )

    assert view.status == "succeeded", view.error
    assert view.message == STEP_DONE
    assert view.detail[EMBEDDINGS_STALE] is True
    assert view.detail["notes"] == 2
    assert view.detail["created"] == 2
    assert indexed_paths(index_path) == {"A.md", "B.md"}
    assert not (index_path.parent / (index_path.name + ".building")).exists()


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def cancel_on_first_write(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Cancel the running job from inside ``index_note``, after the rows are written.

    Returns the ids the indexer actually wrote, so a test can prove the cancellation
    landed *after* real work rather than before it — a cancel that never reached the
    write would make the rollback assertion pass for the wrong reason.
    """
    written: list[str] = []
    original = IndexRepository.index_note

    def cancelling_index_note(self, *args, **kwargs):
        note_id = original(self, *args, **kwargs)
        written.append(note_id)
        current_controller().cancel("user asked to stop")
        return note_id

    monkeypatch.setattr(IndexRepository, "index_note", cancelling_index_note)
    return written


def test_a_cancelled_update_leaves_the_previous_index_untouched(
    tmp_path: Path, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = make_vault(tmp_path, "A.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    note(vault, "B.md")
    note(vault, "C.md")
    written = cancel_on_first_write(monkeypatch)

    view = wait_for(
        runner,
        runner.submit(INDEX_UPDATE_KIND, index_update_job(settings_for(vault, index_path))).job_id,
    )

    assert view.status == "cancelled", view.error
    assert view.error_code is None
    assert view.message == "user asked to stop"
    assert written, "the cancellation never reached the write, so this proves nothing"
    assert indexed_paths(index_path) == {"A.md"}


def test_a_cancelled_update_releases_the_write_lock(
    tmp_path: Path, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job runs on another thread, so a leaked lock would still block this one."""
    vault = make_vault(tmp_path, "A.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    note(vault, "B.md")
    note(vault, "C.md")
    cancel_on_first_write(monkeypatch)

    view = wait_for(
        runner,
        runner.submit(INDEX_UPDATE_KIND, index_update_job(settings_for(vault, index_path))).job_id,
    )
    assert view.status == "cancelled", view.error

    with vault_lock(index_path, operation="probe", timeout=0):
        pass


def test_a_rebuild_cancelled_during_the_scan_leaves_the_previous_index(
    tmp_path: Path, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first checkpoint is enough: no shadow database is ever created."""
    vault = make_vault(tmp_path, "A.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    note(vault, "B.md")
    before = index_path.stat().st_mtime_ns

    import obsai.application.index_jobs as module

    real_scan = module.scan_markdown_files

    def scanning_then_cancelling(root: Path):
        found = real_scan(root)
        current_controller().cancel("user asked to stop")
        return found

    monkeypatch.setattr(module, "scan_markdown_files", scanning_then_cancelling)

    view = wait_for(
        runner,
        runner.submit(INDEX_REBUILD_KIND, index_rebuild_job(settings_for(vault, index_path))).job_id,
    )

    assert view.status == "cancelled", view.error
    assert indexed_paths(index_path) == {"A.md"}
    assert index_path.stat().st_mtime_ns == before
    assert not (index_path.parent / (index_path.name + ".building")).exists()


def test_a_rebuild_cancelled_at_the_swap_leaves_a_complete_live_index(
    tmp_path: Path, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live file is replaced by one ``os.replace``, so there is no third outcome.

    A cancel arriving at that moment lands either before it — the previous index is
    still there — or after it, with the already-validated shadow index live. Nothing
    in between can leave a partly-written database, which is the whole of what "an
    interrupted rebuild does not damage the live index" means. The job still reports
    ``cancelled``: a decision the user made is not a success either.

    The cancel is raised from inside the swap rather than before it because that is
    the moment the property is about, and ``swaps`` proves it really was raised there
    rather than somewhere earlier where the assertion would pass for another reason.
    """
    vault = make_vault(tmp_path, "A.md")
    index_path = tmp_path / "index.db"
    build_index(vault, index_path)
    note(vault, "B.md")

    from obsai.indexing import rebuild as rebuild_module

    real_sync = rebuild_module._sync_directory
    swaps: list[Path] = []

    def syncing_then_cancelling(path: Path) -> None:
        real_sync(path)
        swaps.append(path)
        current_controller().cancel("user asked to stop")

    monkeypatch.setattr(rebuild_module, "_sync_directory", syncing_then_cancelling)

    view = wait_for(
        runner,
        runner.submit(INDEX_REBUILD_KIND, index_rebuild_job(settings_for(vault, index_path))).job_id,
    )

    assert view.status == "cancelled", view.error
    assert swaps, "the swap never ran, so this proves nothing"
    assert indexed_paths(index_path) == {"A.md", "B.md"}
    assert not (index_path.parent / (index_path.name + ".building")).exists()
    with vault_lock(index_path, operation="probe", timeout=0):
        pass
