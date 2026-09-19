"""C-5 acceptance: cooperative cancellation of embedding jobs.

Pinned behaviours:
* Cancellation stops between batches: the in-flight batch finishes, the next never starts.
* Cancellation leaves the index consistent: NO half-written vectors are inserted into SQLite.
* Cancelled jobs report status="cancelled", the cancellation reason as message, and error=None.
* The write lock (vault_lock) is cleanly released on cancellation.
* Cancelling a queued job marks it cancelled without executing any work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from obsai.application.embedding_jobs import (
    approve_embedding_plan,
    create_embedding_plan,
    embedding_job,
    reset_plan_store,
)
from obsai.application.jobs import (
    CancellationToken,
    JobContext,
    JobRunner,
)
from obsai.application.locks import vault_lock
from obsai.application.paths import database_path
from obsai.config.loader import load_settings
from obsai.config.models import EmbeddingConfig, Settings
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.openai_provider import OpenAIEmbeddingProvider
from obsai.indexing import IncrementalIndexer
from obsai.shutdown import check_shutdown, current_controller
from obsai.storage import Database, IndexRepository


@pytest.fixture(autouse=True)
def clean_plans():
    reset_plan_store()
    yield
    reset_plan_store()


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


def make_vault(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_text(f"# {name}\n\nContent for note {name}.\n", encoding="utf-8")
    return root


def write_config(
    tmp_path: Path,
    vault: Path,
    db_path: Path,
    *,
    batch_size: int = 1,
) -> None:
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{db_path}"\n'
        f'[embedding]\nprovider = "openai"\nmodel = "text-embedding-3-small"\n'
        f"batch_size = {batch_size}\nmax_concurrency = 1\n",
        encoding="utf-8",
    )


def populate_index(vault: Path, db_path: Path) -> None:
    with Database(db_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)


def test_cancelling_embedding_job_stops_between_batches_without_half_written_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: JobRunner
) -> None:
    """C-5 core acceptance:

    When an embedding job is cancelled after batch 1:
    - Subsequent batches are NOT requested.
    - No half-written vectors are inserted into SQLite.
    - Job is reported as cancelled, not failed.
    - Write lock is released.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "note1.md", "note2.md", "note3.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path, batch_size=1)
    populate_index(vault, db_path)

    settings = load_settings()
    plan_view = create_embedding_plan(settings)
    assert plan_view.request_count >= 3, "need at least 3 batches to prove batch edge stop"

    requested_batches: list[list[str]] = []

    async def fake_embed(self, texts: list[str]) -> list[list[float]]:
        requested_batches.append(texts)
        if len(requested_batches) == 1:
            # Cancel after the first batch finishes
            ctrl = current_controller()
            assert isinstance(ctrl, CancellationToken)
            ctrl.cancel("user stopped generation")
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed", fake_embed)

    job_view = approve_embedding_plan(plan_view.plan_id, plan_view.nonce, settings, runner)

    finished = wait_for(runner, job_view.job_id)

    assert finished.status == "cancelled"
    assert finished.message == "user stopped generation"
    assert finished.error is None
    assert finished.error_code is None

    # Stopped between batches: batch 1 was requested, but batches 2 and 3 were never requested
    assert len(requested_batches) == 1

    # Database consistency: NO half-written vectors were committed!
    with Database(db_path) as db:
        cursor = db.connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='chunk_embeddings'"
        )
        table_exists = cursor.fetchone()[0] > 0
        if table_exists:
            count = db.connection.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0]
            assert count == 0, f"Expected 0 vectors committed after cancellation, found {count}"

    # Lock is released: we can acquire vault_lock immediately without timeout
    with vault_lock(db_path, operation="test lock release", timeout=1.0):
        pass


def test_cancelling_queued_embedding_job_never_starts_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job cancelled while queued stops before work starts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    settings = load_settings()
    plan_entry = create_embedding_plan(settings)

    work_started = threading.Event()
    blocker_gate = threading.Event()

    def blocking_work(context: JobContext) -> dict[str, Any]:
        blocker_gate.wait(timeout=5)
        return {"blocker": True}

    def work(context: JobContext) -> dict[str, Any]:
        work_started.set()
        return {"done": True}

    with JobRunner() as runner:
        # Submit blocking job first to saturate the single worker
        b_view = runner.submit("blocker", blocking_work)
        time.sleep(0.05)

        # Submit embedding job which will sit in "queued" status
        queued_view = runner.submit("embedding", work)
        assert runner.get(queued_view.job_id).status == "queued"

        # Cancel while queued
        assert runner.cancel(queued_view.job_id, "cancelled in queue") is True

        # Unblock first job
        blocker_gate.set()

        wait_for(runner, b_view.job_id)
        finished = wait_for(runner, queued_view.job_id)

        assert finished.status == "cancelled"
        assert finished.message == "cancelled in queue"
        assert finished.error is None
        assert not work_started.is_set(), "work() should not have been called for cancelled queued job"
