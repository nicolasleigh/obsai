"""B-1 acceptance: the dependencies hand out what a route needs, and clean up.

Two properties are pinned here that no endpoint test would catch:

* the index connection is closed when the request ends — a leak would hold the
  index open for the lifetime of the process, and on Windows would block a rebuild;
* the lock factory really takes the lock, so a write route that uses it later
  cannot be silently unguarded.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from obsai.api.deps import AppState, get_app_state, get_settings, get_write_lock, index_handle
from obsai.application.locks import is_locked
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository


def built_index(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def test_settings_come_from_the_isolated_home() -> None:
    """The conftest fixture points HOME at a temporary directory, so this asserts
    the dependency reads configuration rather than inventing defaults."""
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.vault.path is None


def test_uptime_is_measured_from_the_recorded_start() -> None:
    state = AppState(started_at=datetime.now(timezone.utc) - timedelta(seconds=3))
    assert 3 <= state.uptime_seconds < 30


def test_the_app_state_is_reachable_from_a_request() -> None:
    app = FastAPI()
    app.state.obsai = AppState(started_at=datetime(2026, 9, 14, tzinfo=timezone.utc))

    @app.get("/state")
    def read(request: Request) -> dict[str, str]:
        return {"started_at": get_app_state(request).started_at.isoformat()}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/state").json() == {"started_at": "2026-09-14T00:00:00+00:00"}


def test_the_index_dependency_opens_and_closes(tmp_path: Path) -> None:
    vault, database_path = built_index(tmp_path)
    generator = index_handle(Settings(vault={"path": vault}, index={"database": database_path}))
    handle = next(generator)
    connection = handle.require().connection
    generator.close()
    assert handle.database is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_the_index_dependency_yields_a_closed_handle_when_absent(tmp_path: Path) -> None:
    generator = index_handle(Settings(index={"database": tmp_path / "absent.db"}))
    handle = next(generator)
    generator.close()
    assert handle.usable is False
    assert handle.exists is False


def test_the_lock_factory_actually_locks(tmp_path: Path) -> None:
    vault, database_path = built_index(tmp_path)
    acquire = get_write_lock(Settings(vault={"path": vault}, index={"database": database_path}))
    assert is_locked(database_path) is False
    with acquire("note update"):
        assert is_locked(database_path) is True
    assert is_locked(database_path) is False


def test_the_lock_factory_is_re_entrant(tmp_path: Path) -> None:
    """``organizer.apply`` nests inside a transaction commit; a non-re-entrant lock
    would deadlock the request that needs it."""
    vault, database_path = built_index(tmp_path)
    acquire = get_write_lock(Settings(vault={"path": vault}, index={"database": database_path}))
    with acquire("outer"):
        with acquire("inner"):
            assert is_locked(database_path) is True
    assert is_locked(database_path) is False
