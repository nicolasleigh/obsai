"""C-2: the job endpoints answer about the right index, and 404 says "job".

Three things are pinned here that no other test would catch.

**Reading writes nothing.** ``GET /jobs`` on an index that has never run a job must
leave no journal behind. The stream re-reads twice a second, so this is the read-only
guarantee under the heaviest read path in the project.

**The runner follows the configuration.** A registry keyed by the index is the whole
reason ``/jobs`` is not a single process-wide store: pointing ``index.database`` at
another index has to change the answer. ``test_the_answer_follows_the_configuration``
edits the config file between two requests and asserts it does.

**A missing job is not a missing note.** ``job_not_found`` rather than ``not_found``:
``web/src/lib/errors.ts`` turns codes into sentences, and "this note is not in the
index" is the wrong sentence here. The status still comes from ``NotFoundError``
through the MRO, which is what ``test_the_status_code_still_comes_from_the_parent``
checks — the subclass changes the wording, not the code.

The stream itself is exercised in ``tests/integration/test_api_events.py`` over a real
socket. ``TestClient`` buffers the whole response body before handing it back, so it
cannot observe a stream that never ends — that is not a limitation of this endpoint,
it is a limitation of the client, and testing through it would prove nothing.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.routes.jobs import JOB_EVENT, SNAPSHOT_EVENT, job_events
from obsai.application.dto import JobView
from obsai.application.jobs import JobRunner, JobStore, job_journal_path
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def build_index(tmp_path: Path, name: str) -> Path:
    vault = tmp_path / f"vault-{name}"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / f"{name}.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return database_path


def write_config(tmp_path: Path, database_path: Path) -> None:
    vault = database_path.parent / f"vault-{database_path.stem}"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )


def listing(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def slow_job(steps: int = 200, delay: float = 0.02):
    def work(context) -> dict[str, int]:
        for step in range(steps):
            context.progress("step", step=step)
            time.sleep(delay)
        return {"steps": steps}

    return work


def saved_job(job_id: str) -> JobView:
    return JobView(job_id=job_id, kind="embedding", status="succeeded", created_at=NOW)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def test_reading_jobs_writes_nothing(tmp_path: Path) -> None:
    database_path = build_index(tmp_path, "a")
    write_config(tmp_path, database_path)
    before = listing(tmp_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/jobs").json() == []
        assert client.get("/api/v1/jobs").json() == []

    assert not job_journal_path(database_path).exists()
    assert listing(tmp_path) == before


def test_the_answer_follows_the_configuration(tmp_path: Path) -> None:
    """A registry keyed by the index, not one store for the process."""
    first = build_index(tmp_path, "a")
    second = build_index(tmp_path, "b")
    with JobStore(job_journal_path(second)) as store:
        store.save(saved_job("from-b"))

    write_config(tmp_path, first)
    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/jobs").json() == []

        write_config(tmp_path, second)
        payload = client.get("/api/v1/jobs").json()

    assert [item["job_id"] for item in payload] == ["from-b"]


# --------------------------------------------------------------------------- #
# One job
# --------------------------------------------------------------------------- #


def test_an_unknown_job_is_a_404_that_names_a_job(tmp_path: Path) -> None:
    database_path = build_index(tmp_path, "a")
    write_config(tmp_path, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.get("/api/v1/jobs/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"
    assert response.json()["error"]["type"] == "JobNotFoundError"


def test_the_status_code_still_comes_from_the_parent(tmp_path: Path) -> None:
    """The subclass changes the wording, not the status: 404 is inherited."""
    from obsai.api.errors import http_status
    from obsai.application.jobs import JobNotFoundError
    from obsai.errors import NotFoundError

    assert http_status(JobNotFoundError("x")) == http_status(NotFoundError("x")) == 404


def test_a_finished_job_is_readable_and_cancelling_it_changes_nothing(tmp_path: Path) -> None:
    """Cancelling something already over is the honest reply, not a 409."""
    database_path = build_index(tmp_path, "a")
    write_config(tmp_path, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        runner = client.app.state.jobs.runner_for(database_path)
        submitted = runner.submit("probe", lambda context: {"notes": 1})
        wait_until(lambda: runner.get(submitted.job_id).terminal)

        read = client.get(f"/api/v1/jobs/{submitted.job_id}")
        cancelled = client.post(f"/api/v1/jobs/{submitted.job_id}/cancel")

    assert read.status_code == 200
    assert read.json()["status"] == "succeeded"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "succeeded"


def test_cancelling_a_running_job_asks_it_to_stop(tmp_path: Path) -> None:
    database_path = build_index(tmp_path, "a")
    write_config(tmp_path, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        runner = client.app.state.jobs.runner_for(database_path)
        submitted = runner.submit("probe", slow_job())
        wait_until(lambda: runner.get(submitted.job_id).status == "running")

        response = client.post(f"/api/v1/jobs/{submitted.job_id}/cancel")
        assert response.status_code == 200

        wait_until(lambda: runner.get(submitted.job_id).status == "cancelled")
        final = client.get(f"/api/v1/jobs/{submitted.job_id}").json()

    assert final["status"] == "cancelled"
    assert final["finished_at"] is not None


def test_cancelling_an_unknown_job_is_the_same_404(tmp_path: Path) -> None:
    database_path = build_index(tmp_path, "a")
    write_config(tmp_path, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/jobs/nope/cancel")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


# --------------------------------------------------------------------------- #
# The stream's shape, driven directly
# --------------------------------------------------------------------------- #


def drain(runner: JobRunner, count: int, *, poll_seconds: float = 0.01):
    """Pull ``count`` events out of the stream without an HTTP client."""

    async def take():
        generator = job_events(runner, poll_seconds=poll_seconds)
        try:
            return [await anext(generator) for _ in range(count)]
        finally:
            await generator.aclose()

    return asyncio.run(take())


def test_the_stream_opens_with_a_snapshot(tmp_path: Path) -> None:
    """Every connection starts with the whole world, so a reconnect is correct."""
    with JobRunner(database_path=tmp_path / "index.db") as runner:
        submitted = runner.submit("probe", lambda context: {"notes": 1})
        wait_until(lambda: runner.get(submitted.job_id).terminal)
        first = drain(runner, 1)[0]

    assert first.event == SNAPSHOT_EVENT
    assert submitted.job_id in first.data
    assert "succeeded" in first.data


def test_the_stream_does_not_repeat_an_unchanged_job(tmp_path: Path) -> None:
    """A state feed, not a heartbeat: silence means nothing changed.

    Deterministic rather than timing-dependent: the job is finished before the
    stream opens, so no pass can produce an event and the wait always expires.
    """

    async def expect_silence():
        with JobRunner(database_path=tmp_path / "index.db") as runner:
            submitted = runner.submit("probe", lambda context: None)
            wait_until(lambda: runner.get(submitted.job_id).terminal)
            generator = job_events(runner, poll_seconds=0.01)
            try:
                await anext(generator)  # the snapshot
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(anext(generator), timeout=0.2)
            finally:
                await generator.aclose()

    asyncio.run(expect_silence())


def test_the_stream_reports_a_change_as_a_job_event(tmp_path: Path) -> None:
    """The snapshot is followed by one event per change, not by another snapshot.

    One generator for the whole test: a second ``drain`` would open a second stream,
    whose first event is a snapshot again — and the assertion would pass for the
    wrong reason.
    """

    async def watch():
        with JobRunner(database_path=tmp_path / "index.db") as runner:
            generator = job_events(runner, poll_seconds=0.01)
            try:
                snapshot = await anext(generator)
                submitted = runner.submit("probe", lambda context: {"notes": 1})
                change = await asyncio.wait_for(anext(generator), timeout=5)
            finally:
                await generator.aclose()
        return snapshot, submitted, change

    snapshot, submitted, change = asyncio.run(watch())

    assert snapshot.event == SNAPSHOT_EVENT
    assert snapshot.data == "[]"
    assert change.event == JOB_EVENT
    assert submitted.job_id in change.data
