"""B-1 acceptance: opening the index never turns a degraded state into a crash.

The handle is the seam that lets ``/status`` describe a missing or broken index and
lets every data route fail loudly when it needs one. Both halves are tested here,
because getting only one of them right is the easy mistake: a handle that swallows
every failure makes data routes silently return nothing.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from obsai.application.index import IndexHandle, open_index
from obsai.application.paths import MISSING_INDEX_MESSAGE
from obsai.config.models import Settings
from obsai.errors import ConfigError
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


def settings_for(database_path: Path) -> Settings:
    return Settings(index={"database": database_path})


def test_a_missing_index_is_not_a_failure(tmp_path: Path) -> None:
    handle = open_index(settings_for(tmp_path / "absent.db"))
    assert handle.path == tmp_path / "absent.db"
    assert handle.exists is False
    assert handle.usable is False
    assert handle.failure is None
    assert handle.error is None


def test_requiring_a_missing_index_uses_the_cli_message(tmp_path: Path) -> None:
    """One wording for "the index has not been built", shared with the terminal."""
    handle = open_index(settings_for(tmp_path / "absent.db"))
    with pytest.raises(ConfigError) as raised:
        handle.require()
    assert str(raised.value) == MISSING_INDEX_MESSAGE


def test_an_existing_index_is_opened(tmp_path: Path) -> None:
    vault, database_path = built_index(tmp_path)
    handle = open_index(settings_for(database_path))
    try:
        assert handle.exists is True
        assert handle.usable is True
        assert handle.error is None
        assert handle.require().connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    finally:
        handle.close()


def test_an_unreadable_index_keeps_the_original_failure(tmp_path: Path) -> None:
    database_path = tmp_path / "index.db"
    database_path.write_bytes(b"not a database")
    handle = open_index(settings_for(database_path))
    assert handle.exists is True
    assert handle.usable is False
    assert handle.failure is not None
    assert handle.error == str(handle.failure)


def test_requiring_an_unreadable_index_reraises_its_own_error(tmp_path: Path) -> None:
    """A corrupt index is a server-side problem, not a 400: it must not be
    flattened into the "not built yet" message."""
    database_path = tmp_path / "index.db"
    database_path.write_bytes(b"not a database")
    handle = open_index(settings_for(database_path))
    assert handle.failure is not None
    with pytest.raises(type(handle.failure)) as raised:
        handle.require()
    assert not isinstance(raised.value, ConfigError)
    assert str(raised.value) == handle.error


def test_an_unsafe_path_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A directory where the index should be is a state, not an exception."""
    database_path = tmp_path / "index.db"
    database_path.mkdir()
    handle = open_index(settings_for(database_path))
    assert handle.exists is True
    assert handle.usable is False
    assert handle.error


def test_closing_is_idempotent_and_safe_without_a_connection(tmp_path: Path) -> None:
    handle = open_index(settings_for(tmp_path / "absent.db"))
    handle.close()
    handle.close()
    assert handle.database is None

    vault, database_path = built_index(tmp_path)
    opened = open_index(settings_for(database_path))
    opened.close()
    opened.close()
    assert opened.database is None


def in_another_thread(action) -> BaseException | None:
    """Run ``action`` on a thread of its own and hand back what it raised.

    This is not a stress test: it is the shape FastAPI's threadpool actually uses.
    ``contextmanager_in_threadpool`` submits a dependency's ``__enter__`` and its
    ``__exit__`` as two separate jobs, and anyio is free to run them on different
    workers.
    """
    failures: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - returned, not swallowed
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return failures[0] if failures else None


def test_the_handle_is_usable_from_a_thread_that_did_not_open_it(tmp_path: Path) -> None:
    """A request owns the handle; it does not own the thread it happens to run on.

    Reading through the handle and closing it both have to survive landing on a
    different worker than the one that built it. When they did not, the failure
    surfaced inside the cleanup — ``handle.close()`` raising
    ``SQLite objects created in a thread can only be used in that same thread`` —
    which replaced whatever the handler was about to return with a 500. A missing
    note was the visible symptom: a 404 about half the time, a 500 the rest.
    """
    vault, database_path = built_index(tmp_path)
    handle = open_index(settings_for(database_path))
    try:
        assert in_another_thread(
            lambda: handle.require().connection.execute("SELECT COUNT(*) FROM notes").fetchone()
        ) is None
        assert in_another_thread(handle.close) is None
    finally:
        handle.close()
    assert handle.database is None


def test_the_handle_is_not_part_of_the_wire_contract() -> None:
    """It carries a live ``Database``, so it must never be serialisable."""
    from pydantic import BaseModel

    from obsai.application import dto

    assert not hasattr(dto, "IndexHandle")
    assert not issubclass(IndexHandle, BaseModel)
