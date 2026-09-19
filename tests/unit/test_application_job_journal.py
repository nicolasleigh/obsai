"""C-1 acceptance: the journal outlives the process, and never holds note text.

Three properties.

**A job id survives a restart.** ``JobRunner.get`` falls back to the journal, so the
id a caller was handed before the restart is still the id it can ask about after
it. ``test_a_job_id_still_resolves_after_the_runner_is_recreated`` is the whole
acceptance in one test: a second store and a second runner, reading the first
runner's job.

**A job that was running is ``interrupted``, not ``succeeded``.** The status exists
because the other two candidates both lie: ``succeeded`` claims work that may not
have finished, and ``cancelled`` blames a user who never asked for anything.

**The journal is not a second copy of the Vault.** ``save`` keeps scalars and drops
long strings, so a job that hands note text to ``progress()`` cannot put it on
disk. That is asserted against the journal's bytes, not against the loaded view —
the point is what is on disk.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obsai.application.dto import JobView
from obsai.application.jobs import (
    MAX_ERROR_TEXT,
    JobNotFoundError,
    JobRunner,
    JobStore,
    job_journal_path,
    open_job_journal,
    recover_interrupted_jobs,
)
from obsai.errors import SchemaError
from obsai.storage import Database

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def journal_path(tmp_path: Path) -> Path:
    return job_journal_path(tmp_path / "index.db")


def view(job_id: str, status: str, **overrides) -> JobView:
    fields = {
        "job_id": job_id,
        "kind": "index-update",
        "status": status,
        "created_at": NOW,
        "started_at": NOW,
    }
    fields.update(overrides)
    return JobView(**fields)


def wait_for(runner: JobRunner, job_id: str, *, timeout: float = 10.0) -> JobView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runner.get(job_id)
        if result.terminal:
            return result
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {runner.get(job_id)}")


# --------------------------------------------------------------------------- #
# The journal is its own file with its own version
# --------------------------------------------------------------------------- #


def test_the_journal_gets_its_own_schema_version(tmp_path: Path) -> None:
    with JobStore(journal_path(tmp_path)) as store:
        tables = {
            row[0]
            for row in store._database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = store._database.connection.execute("PRAGMA user_version").fetchone()[0]
    assert "jobs" in tables
    assert version == 1


def test_the_journal_refuses_to_be_pointed_at_an_index(tmp_path: Path) -> None:
    index = tmp_path / "index.db"
    with Database(index):
        pass
    with pytest.raises(SchemaError, match="Unsupported job journal schema version: 3"):
        JobStore(index)


def test_the_journal_refuses_an_unversioned_non_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "index.jobs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE something (id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaError, match="Unversioned job journal is not empty"):
        JobStore(path)


def test_the_journal_is_a_sibling_of_the_index_not_a_table_inside_it(tmp_path: Path) -> None:
    index = tmp_path / "index.db"
    assert job_journal_path(index) == tmp_path / "index.jobs.db"
    with Database(index) as database:
        tables = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "jobs" not in tables


# --------------------------------------------------------------------------- #
# What the journal is allowed to hold
# --------------------------------------------------------------------------- #


def test_note_text_never_reaches_the_journal(tmp_path: Path) -> None:
    store = JobStore(journal_path(tmp_path))
    note_text = "# SECRETNOTE\n\n" + "x" * 4000
    store.save(
        view(
            "j1",
            "running",
            message="Indexing",
            detail={
                "notes": 12,
                "path": "Notes/Redis.md",
                "done": True,
                "missing": None,
                "content": note_text,
                "blocks": ["a", "b"],
                "nested": {"raw": note_text},
            },
        )
    )
    loaded = store.load("j1")
    on_disk = store.path.read_bytes()
    store.close()

    assert loaded.detail == {
        "done": True,
        "missing": None,
        "notes": 12,
        "path": "Notes/Redis.md",
    }
    assert b"SECRETNOTE" not in on_disk, "note text reached the journal file"


def test_a_long_error_is_truncated_and_its_code_is_kept_apart(tmp_path: Path) -> None:
    store = JobStore(journal_path(tmp_path))
    store.save(
        view(
            "j1",
            "failed",
            error="EmbeddingError: " + "y" * 4000,
            error_code="EmbeddingError",
        )
    )
    loaded = store.load("j1")
    store.close()

    assert loaded.error_code == "EmbeddingError"
    assert loaded.error is not None
    assert loaded.error.startswith("EmbeddingError: ")
    assert loaded.error.endswith("…")
    assert len(loaded.error) <= MAX_ERROR_TEXT + 1


def test_a_saved_job_round_trips_unchanged(tmp_path: Path) -> None:
    store = JobStore(journal_path(tmp_path))
    original = view(
        "j1",
        "failed",
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
        message="Embedding 3/10",
        detail={"notes": 3, "cost_usd": 1.5e-06},
        error="ConfigError: no key",
        error_code="ConfigError",
    )
    store.save(original)
    loaded = store.load("j1")
    store.close()

    assert loaded == original


# --------------------------------------------------------------------------- #
# Restart: the job id still resolves
# --------------------------------------------------------------------------- #


def test_a_job_id_still_resolves_after_the_runner_is_recreated(tmp_path: Path) -> None:
    path = journal_path(tmp_path)

    with JobRunner(store=JobStore(path)) as runner:
        submitted = runner.submit("index-update", lambda context: {"notes": 2})
        finished = wait_for(runner, submitted.job_id)
    assert finished.status == "succeeded"

    # A second process does exactly this: open the journal and ask for the id.
    with JobRunner(store=JobStore(path)) as runner:
        recovered = runner.get(finished.job_id)
        assert recovered.status == "succeeded"
        assert recovered.kind == "index-update"
        assert recovered.detail["notes"] == 2
        assert recovered.finished_at == finished.finished_at


def test_a_progress_report_right_after_start_reaches_the_journal(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    reported = threading.Event()
    release = threading.Event()

    def work(context):
        context.progress("Embedding batch 1", notes=1)
        reported.set()
        release.wait(timeout=10)
        return None

    with JobRunner(store=JobStore(path)) as runner:
        submitted = runner.submit("embedding", work)
        assert reported.wait(timeout=10)
        # The job is still running, so only the journal can answer what it is
        # doing — and a job that hangs here is exactly the one worth asking about.
        with JobStore(path) as reader:
            live = reader.load(submitted.job_id)
        release.set()
        wait_for(runner, submitted.job_id)

    assert live.status == "running"
    assert live.message == "Embedding batch 1"
    assert live.detail["notes"] == 1


def test_a_job_is_journaled_before_it_can_start(tmp_path: Path) -> None:
    """The id must be resolvable from the journal even if the process dies first.

    The write in ``submit`` is invisible on the happy path, because the worker
    overwrites the same row a moment later. What it actually buys is the window
    between handing out an id and the job starting, so the test has to hold the
    single worker busy to see a job that is still ``queued``.
    """
    path = journal_path(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking(context):
        started.set()
        release.wait(timeout=10)
        return None

    with JobRunner(store=JobStore(path)) as runner:
        runner.submit("first", blocking)
        assert started.wait(timeout=10)
        queued = runner.submit("second", lambda context: {"notes": 9})
        with JobStore(path) as reader:
            assert reader.load(queued.job_id).status == "queued"
        release.set()
        wait_for(runner, queued.job_id)


def test_the_terminal_write_carries_the_last_progress_message(tmp_path: Path) -> None:
    path = journal_path(tmp_path)

    def work(context):
        context.progress("Embedding batch 1", notes=1)
        context.progress("Embedding batch 2", notes=2)
        return {"notes": 2}

    with JobRunner(store=JobStore(path)) as runner:
        submitted = runner.submit("embedding", work)
        wait_for(runner, submitted.job_id)

    with JobStore(path) as store:
        loaded = store.load(submitted.job_id)
    assert loaded.status == "succeeded"
    assert loaded.message == "Embedding batch 2"
    assert loaded.detail["notes"] == 2


def test_the_live_view_wins_over_the_journal_row(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    with JobRunner(store=JobStore(path)) as runner:
        submitted = runner.submit("index-update", lambda context: {"notes": 1})
        finished = wait_for(runner, submitted.job_id)
        assert runner.get(submitted.job_id) == finished
        assert [item.job_id for item in runner.list()] == [submitted.job_id]


def test_an_unknown_job_is_still_rejected(tmp_path: Path) -> None:
    with JobRunner(store=JobStore(journal_path(tmp_path))) as runner:
        with pytest.raises(JobNotFoundError, match="Unknown job"):
            runner.get("nope")


# --------------------------------------------------------------------------- #
# Interrupted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
def test_a_job_left_open_by_a_dead_process_becomes_interrupted(
    tmp_path: Path, status: str
) -> None:
    path = journal_path(tmp_path)
    with JobStore(path) as store:
        store.save(view("j1", status))

    assert recover_interrupted_jobs(tmp_path / "index.db") == 1

    with JobStore(path) as store:
        loaded = store.load("j1")
    assert loaded.status == "interrupted"
    assert loaded.terminal
    assert loaded.finished_at is not None
    assert loaded.message is not None and "Interrupted" in loaded.message


def test_recovery_leaves_finished_jobs_alone(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    finished = ["succeeded", "failed", "cancelled", "interrupted"]
    with JobStore(path) as store:
        for index, status in enumerate(finished):
            store.save(view(f"j{index}", status))

    assert recover_interrupted_jobs(tmp_path / "index.db") == 0

    with JobStore(path) as store:
        assert [store.load(f"j{index}").status for index in range(len(finished))] == finished


def test_recovery_of_a_missing_journal_creates_nothing(tmp_path: Path) -> None:
    assert recover_interrupted_jobs(tmp_path / "index.db") == 0
    assert open_job_journal(tmp_path / "index.db") is None
    assert list(tmp_path.iterdir()) == [], "recovery created something on a read-only path"


# --------------------------------------------------------------------------- #
# The journal is not the job
# --------------------------------------------------------------------------- #


def test_a_job_that_cannot_be_journaled_is_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(journal_path(tmp_path))
    ran = threading.Event()

    def explode(self, view: JobView) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(JobStore, "save", explode)
    with JobRunner(store=store) as runner:
        with pytest.raises(sqlite3.OperationalError):
            runner.submit("index-update", lambda context: ran.set() or None)
    assert not ran.is_set()


def test_a_journal_write_failure_does_not_turn_finished_work_into_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(journal_path(tmp_path))
    real_save = JobStore.save
    calls = {"count": 0}

    def flaky(self, view: JobView) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise sqlite3.OperationalError("disk I/O error")
        real_save(self, view)

    monkeypatch.setattr(JobStore, "save", flaky)
    with JobRunner(store=store) as runner:
        submitted = runner.submit("index-update", lambda context: {"notes": 1})
        finished = wait_for(runner, submitted.job_id)

    assert finished.status == "succeeded"
    assert "disk I/O error" in finished.detail["journal_error"]
