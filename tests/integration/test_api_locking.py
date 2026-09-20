"""C-6 acceptance: Cross-process lock integration between API and CLI, and SQLite busy_timeout.

Pinned behaviours:
* When a CLI or other process holds the Vault write lock, mutating API endpoints fail fast
  with HTTP 423 Locked and error code "lock_busy".
* Refused routes: POST /jobs/index-update, POST /jobs/index-rebuild, POST /embedding/plans/{id}/approve.
* Once the write lock is released, those same endpoints accept requests (HTTP 202 Accepted).
* SQLite transactions configured with busy_timeout convert locked contention to LockBusyError.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.application.embedding_jobs import reset_plan_store
from obsai.application.locks import LockBusyError, is_locked, vault_lock
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def clean_plans():
    reset_plan_store()
    yield
    reset_plan_store()


def make_vault(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_text(f"# {name}\n\nBody for {name}.\n", encoding="utf-8")
    return root


def write_config(tmp_path: Path, vault: Path, database_path: Path) -> None:
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n'
        f'[embedding]\nprovider = "openai"\nmodel = "text-embedding-3-small"\n',
        encoding="utf-8",
    )


def populate_index(vault: Path, database_path: Path) -> None:
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)


def test_api_write_routes_fail_fast_with_423_when_vault_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md", "B.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    app = create_app()

    # Pre-generate an embedding plan before locking
    with TestClient(app, base_url=BASE_URL) as client:
        plan_res = client.post("/api/v1/embedding/plans")
        assert plan_res.status_code == 200
        plan = plan_res.json()

    # Now acquire the cross-process vault_lock (simulating CLI "obsai index update")
    with vault_lock(database_path, operation="cli index update"):
        assert is_locked(database_path) is True

        with TestClient(app, base_url=BASE_URL) as client:
            # 1. POST /jobs/index-update must be refused with 423
            res_update = client.post("/api/v1/jobs/index-update")
            assert res_update.status_code == 423
            assert res_update.json()["error"]["code"] == "lock_busy"

            # 2. POST /jobs/index-rebuild must be refused with 423
            res_rebuild = client.post("/api/v1/jobs/index-rebuild")
            assert res_rebuild.status_code == 423
            assert res_rebuild.json()["error"]["code"] == "lock_busy"

            # 3. POST /embedding/plans/{id}/approve must be refused with 423
            res_approve = client.post(
                f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
                json={"nonce": plan["nonce"]},
            )
            assert res_approve.status_code == 423
            assert res_approve.json()["error"]["code"] == "lock_busy"

    # Lock is released: subsequent requests must succeed
    assert is_locked(database_path) is False

    with TestClient(app, base_url=BASE_URL) as client:
        res_after = client.post("/api/v1/jobs/index-update")
        assert res_after.status_code == 202
        assert res_after.json()["kind"] == "index-update"


def test_sqlite_busy_timeout_converts_to_lock_busy_error(tmp_path: Path) -> None:
    """When another connection holds an exclusive lock and timeout expires, Database raises LockBusyError."""
    db_path = tmp_path / "busy_test.db"

    # Set up database with low timeout for testing
    with Database(db_path, timeout=0.1) as db1:
        # Start an exclusive write transaction in raw sqlite to simulate a stuck writer
        raw_conn = sqlite3.connect(str(db_path), isolation_level=None)
        raw_conn.execute("BEGIN EXCLUSIVE")

        try:
            with pytest.raises(LockBusyError, match="busy or locked"):
                with db1.transaction():
                    db1.connection.execute("INSERT OR REPLACE INTO schema_version VALUES (1)")
        finally:
            raw_conn.execute("ROLLBACK")
            raw_conn.close()
