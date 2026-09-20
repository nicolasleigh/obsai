"""Phase C acceptance regression: Tasks and Costs milestone sign-off (C-7).

This suite pins the three core acceptance criteria of Phase C (§6 C-7) plus
startup crash recovery:

1. **取消保留旧索引 (Cancellation preserves old index)**:
   - An index-update cancelled midway rolls back cleanly, preserving existing index facts.
   - An index-rebuild cancelled before atomic swap leaves the original live index intact
     and cleans up any temporary .building shadow database.
   - An embedding job cancelled midway stops at batch boundary, never leaving
     half-written / corrupt vectors in SQLite, and leaves the index completely consistent.
   - Every cancellation cleanly releases the write lock (vault_lock).

2. **预算变化后重新确认 (Budget drift requires re-approval)**:
   - When vault content drifts between embedding plan generation and approval,
     approval is refused with HTTP 409 Conflict (code "plan_drift") with a fresh plan.
   - Stale nonces cannot be replayed or reused.
   - Approval succeeds only when confirming the fresh plan with its fresh nonce.

3. **CLI/UI 互斥生效 (Mutual exclusion between CLI and UI)**:
   - When an external process or CLI holds vault_lock, all UI mutating endpoints
     (POST /jobs/index-update, POST /jobs/index-rebuild, POST /embedding/plans/{id}/approve)
     fail fast with HTTP 423 Locked (code "lock_busy") without enqueuing doomed jobs.
   - When a UI write job is active holding vault_lock, concurrent CLI write commands
     are blocked cleanly with a lock busy error.
   - SQLite connections configured with busy_timeout convert locked contention to LockBusyError.

4. **任务恢复与异常崩溃修复 (Startup crash recovery)**:
   - Server process abnormal crash or restart recovers dangling "running" and "queued"
     jobs on startup, marking them honestly as "interrupted".
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from obsai.api.app import create_app
from obsai.application.dto import EmbeddingPlanView, JobView
from obsai.application.embedding_jobs import (
    approve_embedding_plan,
    create_embedding_plan,
    reset_plan_store,
)
from obsai.application.index_jobs import (
    INDEX_REBUILD_KIND,
    INDEX_UPDATE_KIND,
    index_rebuild_job,
    index_update_job,
)
from obsai.application.jobs import (
    CancellationToken,
    JobRunner,
    JobStore,
    job_journal_path,
)
from obsai.application.locks import LockBusyError, is_locked, vault_lock
from obsai.application.paths import database_path
from obsai.config.loader import load_settings
from obsai.config.models import Settings
from obsai.embedding.openai_provider import OpenAIEmbeddingProvider
from obsai.indexing import IncrementalIndexer
from obsai.indexing import rebuild as rebuild_module
import obsai.application.index_jobs as index_jobs_module
from obsai.shutdown import current_controller
from obsai.storage import Database, IndexRepository

REPO = Path(__file__).resolve().parents[2]
OBSAI = Path(sys.executable).with_name("obsai")
BASE_URL = "http://127.0.0.1:8000"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    reset_plan_store()
    yield
    reset_plan_store()


@pytest.fixture()
def runner():
    with JobRunner() as instance:
        yield instance


def make_vault(tmp_path: Path, *names: str) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for name in names:
        (vault / name).write_text(f"# {name}\n\nContent of {name}.\n", encoding="utf-8")
    return vault


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


def indexed_paths(db_path: Path) -> set[str]:
    with Database(db_path) as database:
        return {record.path for record in IndexRepository(database).notes.list_index_facts()}


def wait_for(runner: JobRunner, job_id: str, *, timeout: float = 10.0) -> JobView:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = runner.get(job_id)
        if view.terminal:
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {runner.get(job_id)}")


# ============================================================================ #
# 1. 取消保留旧索引 (Cancellation Preserves Old Index)
# ============================================================================ #


def test_index_update_cancellation_preserves_old_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: JobRunner
) -> None:
    """An index-update cancelled midway rolls back: old index remains intact and searchable."""
    vault = make_vault(tmp_path, "A.md", "B.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)
    assert indexed_paths(db_path) == {"A.md", "B.md"}

    # Add a new file to vault
    (vault / "C.md").write_text("# C\n\nContent of C.\n", encoding="utf-8")

    # Inject cancellation during incremental update
    real_update = IncrementalIndexer.update

    def cancelling_update(self, root: Path):
        # Cancel right inside update before committing
        current_controller().cancel("user cancelled index update")
        return real_update(self, root)

    monkeypatch.setattr(IncrementalIndexer, "update", cancelling_update)

    settings = load_settings()
    job = runner.submit(INDEX_UPDATE_KIND, index_update_job(settings))
    finished = wait_for(runner, job.job_id)

    assert finished.status == "cancelled"
    assert finished.message == "user cancelled index update"
    assert finished.error is None

    # SQLite transaction rolled back: only A.md and B.md exist, C.md was rolled back!
    assert indexed_paths(db_path) == {"A.md", "B.md"}

    # Write lock must be fully released
    assert is_locked(db_path) is False
    with vault_lock(db_path, operation="probe", timeout=0):
        pass


def test_index_rebuild_cancellation_preserves_old_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: JobRunner
) -> None:
    """An index-rebuild cancelled before swap leaves live index untouched and cleans shadow."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)
    assert indexed_paths(db_path) == {"A.md"}

    # Add a file so a rebuild would have changed things
    (vault / "B.md").write_text("# B\n\nContent of B.\n", encoding="utf-8")
    before_mtime = db_path.stat().st_mtime_ns

    # Inject cancellation during scan before shadow database build
    real_scan = index_jobs_module.scan_markdown_files

    def cancelling_scan(root: Path):
        found = real_scan(root)
        current_controller().cancel("cancelled during rebuild scan")
        return found

    monkeypatch.setattr(index_jobs_module, "scan_markdown_files", cancelling_scan)

    settings = load_settings()
    job = runner.submit(INDEX_REBUILD_KIND, index_rebuild_job(settings))
    finished = wait_for(runner, job.job_id)

    assert finished.status == "cancelled"
    assert finished.message == "cancelled during rebuild scan"
    assert finished.error is None

    # Live index is untouched, no B.md was added
    assert indexed_paths(db_path) == {"A.md"}
    assert db_path.stat().st_mtime_ns == before_mtime

    # Shadow files (.building) must not exist
    shadow_path = db_path.parent / f"{db_path.name}.building"
    assert not shadow_path.exists()

    # Write lock is released
    assert is_locked(db_path) is False


def test_embedding_cancellation_preserves_existing_vectors_without_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: JobRunner
) -> None:
    """Embedding job cancelled midway stops at batch boundary without half-written vectors."""
    vault = make_vault(tmp_path, "n1.md", "n2.md", "n3.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path, batch_size=1)
    populate_index(vault, db_path)

    settings = load_settings()
    plan = create_embedding_plan(settings)
    assert plan.request_count >= 3

    requested_batches: list[list[str]] = []

    async def fake_embed(self, texts: list[str]) -> list[list[float]]:
        requested_batches.append(texts)
        if len(requested_batches) == 1:
            ctrl = current_controller()
            assert isinstance(ctrl, CancellationToken)
            ctrl.cancel("cancel after batch 1")
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed", fake_embed)

    job_view = approve_embedding_plan(plan.plan_id, plan.nonce, settings, runner)
    finished = wait_for(runner, job_view.job_id)

    assert finished.status == "cancelled"
    assert finished.message == "cancel after batch 1"
    assert finished.error is None

    # Batch 1 ran, but subsequent batches were never requested
    assert len(requested_batches) == 1

    # In SQLite, no corrupt or half-written vectors remain
    with Database(db_path) as db:
        cursor = db.connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='chunk_embeddings'"
        )
        if cursor.fetchone()[0] > 0:
            count = db.connection.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0]
            assert count == 0

    # Write lock cleanly released
    assert is_locked(db_path) is False
    with vault_lock(db_path, operation="probe", timeout=0):
        pass


# ============================================================================ #
# 2. 预算变化后重新确认 (Budget Drift Requires Re-approval)
# ============================================================================ #


def test_embedding_plan_drift_requires_reapproval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When vault drifts between plan and approve, approval is rejected with 409 and fresh plan."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        # Step 1: Create initial plan
        res_plan1 = client.post("/api/v1/embedding/plans")
        assert res_plan1.status_code == 200
        plan1 = res_plan1.json()

        # Step 2: Drift vault and index before approval
        (vault / "B.md").write_text("# B\n\nNew file body.\n", encoding="utf-8")
        populate_index(vault, db_path)

        # Step 3: Attempt to approve stale plan1 -> must be refused with 409 plan_drift
        res_approve1 = client.post(
            f"/api/v1/embedding/plans/{plan1['plan_id']}/approve",
            json={"nonce": plan1["nonce"]},
        )
        assert res_approve1.status_code == 409
        err = res_approve1.json()["error"]
        assert err["code"] == "plan_drift"
        assert "details" in err

        fresh_plan = err["details"]
        assert fresh_plan["plan_id"] != plan1["plan_id"]
        assert fresh_plan["chunks_requiring_embeddings"] > plan1["chunks_requiring_embeddings"]

        # Step 4: Stale plan remains dead on retry
        res_retry = client.post(
            f"/api/v1/embedding/plans/{plan1['plan_id']}/approve",
            json={"nonce": plan1["nonce"]},
        )
        assert res_retry.status_code == 409

        # Step 5: Approving the fresh plan with fresh nonce succeeds
        res_approve2 = client.post(
            f"/api/v1/embedding/plans/{fresh_plan['plan_id']}/approve",
            json={"nonce": fresh_plan["nonce"]},
        )
        assert res_approve2.status_code == 202
        job = res_approve2.json()
        assert job["kind"] == "embedding"
        assert job["status"] in ("queued", "running")


def test_embedding_plan_nonce_replay_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonce cannot be reused after approval; replay fails with 409."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        plan = client.post("/api/v1/embedding/plans").json()

        # Invalid nonce on pending plan returns 409 Conflict
        res_invalid = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": "invalid-nonce-123"},
        )
        assert res_invalid.status_code == 409
        assert res_invalid.json()["error"]["code"] == "conflict"

        # First approval succeeds
        res1 = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": plan["nonce"]},
        )
        assert res1.status_code == 202
        job_id = res1.json()["job_id"]

        # Wait for the background job to finish and release the write lock
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            poll = client.get(f"/api/v1/jobs/{job_id}").json()
            if poll["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.02)

        # Replaying the same approval request on the resolved plan returns 409 Conflict
        res2 = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": plan["nonce"]},
        )
        assert res2.status_code == 409
        assert res2.json()["error"]["code"] == "conflict"


# ============================================================================ #
# 3. CLI/UI 互斥生效 (Mutual Exclusion Between CLI and UI)
# ============================================================================ #


def test_external_lock_blocks_all_ui_write_endpoints_with_423(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When external process holds vault_lock, mutating endpoints fail fast with 423 lock_busy."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    app = create_app()
    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post("/api/v1/embedding/plans").json()

    # External lock is acquired (e.g. CLI running index rebuild or transaction)
    with vault_lock(db_path, operation="concurrent CLI execution"):
        assert is_locked(db_path) is True

        with TestClient(app, base_url=BASE_URL) as client:
            # 1. POST /jobs/index-update -> 423
            r_up = client.post("/api/v1/jobs/index-update")
            assert r_up.status_code == 423
            assert r_up.json()["error"]["code"] == "lock_busy"

            # 2. POST /jobs/index-rebuild -> 423
            r_reb = client.post("/api/v1/jobs/index-rebuild")
            assert r_reb.status_code == 423
            assert r_reb.json()["error"]["code"] == "lock_busy"

            # 3. POST /embedding/plans/{id}/approve -> 423
            r_appr = client.post(
                f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
                json={"nonce": plan["nonce"]},
            )
            assert r_appr.status_code == 423
            assert r_appr.json()["error"]["code"] == "lock_busy"

    # Once lock is released, requests are accepted
    assert is_locked(db_path) is False
    with TestClient(app, base_url=BASE_URL) as client:
        r_ok = client.post("/api/v1/jobs/index-update")
        assert r_ok.status_code == 202


@pytest.mark.skipif(not OBSAI.exists(), reason="obsai CLI console script not installed")
def test_active_ui_job_blocks_concurrent_cli_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a UI write job holds vault_lock, concurrent CLI write command fails cleanly."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["COLUMNS"] = "80"

    # Hold vault_lock as a UI job would during execution
    with vault_lock(db_path, operation="UI active job"):
        res = subprocess.run(
            [str(OBSAI), "index", "update"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert res.returncode != 0
        output = " ".join((res.stderr + res.stdout).split())
        assert "already writing to this Vault" in output


def test_sqlite_busy_timeout_prevents_unhandled_crash(tmp_path: Path) -> None:
    """Concurrent SQLite contention raises LockBusyError rather than unhandled crash."""
    db_path = tmp_path / "concurrent.db"

    with Database(db_path, timeout=0.1) as db:
        # Open raw connection and lock file
        raw = sqlite3.connect(str(db_path), isolation_level=None)
        raw.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(LockBusyError, match="busy or locked"):
                with db.transaction():
                    db.connection.execute("INSERT OR REPLACE INTO schema_version VALUES (1)")
        finally:
            raw.execute("ROLLBACK")
            raw.close()


# ============================================================================ #
# 4. 任务恢复与异常崩溃修复 (Startup Crash Recovery)
# ============================================================================ #


def test_server_restart_recovers_interrupted_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On server lifespan startup, unfinished jobs left from a previous crash become 'interrupted'."""
    vault = make_vault(tmp_path, "A.md")
    db_path = tmp_path / "index.db"
    write_config(tmp_path, vault, db_path)
    populate_index(vault, db_path)

    # Write synthetic unclosed jobs to simulate a killed process
    journal = job_journal_path(db_path)
    with JobStore(journal) as store:
        store.save(
            JobView(
                job_id="crashed_job_1",
                kind="index-update",
                status="running",
                created_at=NOW,
                started_at=NOW,
                message="Running update before power loss",
            )
        )
        store.save(
            JobView(
                job_id="crashed_job_2",
                kind="embedding",
                status="queued",
                created_at=NOW,
                started_at=None,
                message="Queued before power loss",
            )
        )
        store.save(
            JobView(
                job_id="completed_job_3",
                kind="index-rebuild",
                status="succeeded",
                created_at=NOW,
                started_at=NOW,
                finished_at=NOW,
                message="Already done",
            )
        )

    # App lifespan startup runs recover_interrupted_jobs
    with TestClient(create_app(), base_url=BASE_URL) as client:
        # Check jobs via API
        r1 = client.get("/api/v1/jobs/crashed_job_1")
        assert r1.status_code == 200
        v1 = r1.json()
        assert v1["status"] == "interrupted"
        assert "Interrupted" in v1["message"]

        r2 = client.get("/api/v1/jobs/crashed_job_2")
        assert r2.status_code == 200
        v2 = r2.json()
        assert v2["status"] == "interrupted"

        r3 = client.get("/api/v1/jobs/completed_job_3")
        assert r3.status_code == 200
        v3 = r3.json()
        assert v3["status"] == "succeeded"
