"""C-2: the journal is opened on demand, and only a write creates it.

The job screens read the journal on every request, and an SSE stream reads it twice
a second. If reading opened it, then opening the UI on a machine that has never run
a job would leave ``index.jobs.db`` behind — and every later read would find a
journal and report "there are no jobs" out of a file it made itself. B-9's rule was
that asking a question must not change the answer; this is the same rule one layer
down, applied to a resource that is created by being opened.

So the runner has two accessors with deliberately different policies: reads take
``open_job_journal`` (open only if it exists), writes take ``JobStore`` (create).
``test_a_journal_that_appears_later_is_still_found`` is what keeps the first policy
honest — a read that cached its own failure would never see the journal that a
submission creates a moment later.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obsai.application.dto import JobView
from obsai.application.jobs import (
    JobNotFoundError,
    JobRunner,
    JobStore,
    job_journal_path,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def wait_for(runner: JobRunner, job_id: str, *, timeout: float = 10.0) -> JobView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = runner.get(job_id)
        if view.terminal:
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {runner.get(job_id)}")


def view(job_id: str) -> JobView:
    return JobView(job_id=job_id, kind="embedding", status="succeeded", created_at=NOW)


def listing(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


# --------------------------------------------------------------------------- #
# Reads create nothing
# --------------------------------------------------------------------------- #


def test_listing_jobs_creates_no_journal(tmp_path: Path) -> None:
    database_path = tmp_path / "index.db"
    before = listing(tmp_path)

    with JobRunner(database_path=database_path) as runner:
        assert runner.list() == []

    assert not job_journal_path(database_path).exists()
    assert listing(tmp_path) == before


def test_asking_for_an_unknown_job_creates_no_journal(tmp_path: Path) -> None:
    database_path = tmp_path / "index.db"
    with JobRunner(database_path=database_path) as runner:
        with pytest.raises(JobNotFoundError):
            runner.get("nope")
    assert not job_journal_path(database_path).exists()


def test_recovery_creates_no_journal(tmp_path: Path) -> None:
    """Recovery runs on every start, including a start that will only ever read."""
    database_path = tmp_path / "index.db"
    with JobRunner(database_path=database_path) as runner:
        assert runner.recover() == 0
    assert not job_journal_path(database_path).exists()


# --------------------------------------------------------------------------- #
# Writes create it
# --------------------------------------------------------------------------- #


def test_a_submission_creates_the_journal_and_a_second_runner_finds_it(tmp_path: Path) -> None:
    database_path = tmp_path / "index.db"
    with JobRunner(database_path=database_path) as runner:
        submitted = runner.submit("probe", lambda context: {"notes": 1})
        assert wait_for(runner, submitted.job_id).status == "succeeded"

    assert job_journal_path(database_path).exists()
    with JobRunner(database_path=database_path) as reopened:
        assert reopened.get(submitted.job_id).status == "succeeded"


def test_a_journal_that_appears_later_is_still_found(tmp_path: Path) -> None:
    """A read that found nothing must not remember that it found nothing."""
    database_path = tmp_path / "index.db"
    with JobRunner(database_path=database_path) as runner:
        assert runner.list() == []

        with JobStore(job_journal_path(database_path)) as store:
            store.save(view("j1"))

        assert [item.job_id for item in runner.list()] == ["j1"]
        assert runner.get("j1").status == "succeeded"


def test_a_refused_submission_leaves_no_queued_record(tmp_path: Path) -> None:
    """``submit`` is strict, so a job it refuses must not linger as ``queued``.

    A record that no worker will ever pick up is worse than no record: it reads as
    work in progress, and it is the one state a reader cannot resolve.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with JobRunner(database_path=blocker / "index.db") as runner:
        with pytest.raises(OSError):
            runner.submit("probe", lambda context: None)
        assert runner.list() == []


# --------------------------------------------------------------------------- #
# The CLI's mode, unchanged
# --------------------------------------------------------------------------- #


def test_a_runner_without_a_journal_still_runs_jobs(tmp_path: Path) -> None:
    """A command that runs one job and exits has nothing to persist."""
    with JobRunner() as runner:
        submitted = runner.submit("probe", lambda context: {"notes": 1})
        assert wait_for(runner, submitted.job_id).status == "succeeded"

    assert listing(tmp_path) == set()
