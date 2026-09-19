"""In-process semantics of the Vault write lock."""

import threading
from pathlib import Path

import pytest

from obsai.application import locks
from obsai.application.locks import LockBusyError, is_locked, lock_path, vault_lock


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    return tmp_path / "state" / "index.db"


def test_lock_path_sits_beside_the_index(database: Path) -> None:
    assert lock_path(database).name == "index.db.lock"
    assert lock_path(database).parent == database.parent


def test_lock_path_normalises_symlinked_parents(tmp_path: Path) -> None:
    """Two spellings of one directory must not produce two lock files."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    assert lock_path(alias / "index.db") == lock_path(real / "index.db")


def test_lock_file_is_created_with_owner_only_permissions(database: Path) -> None:
    with vault_lock(database):
        mode = lock_path(database).stat().st_mode & 0o777
    assert mode == 0o600


def test_lock_is_released_on_exit(database: Path) -> None:
    with vault_lock(database):
        assert is_locked(database)
    assert not is_locked(database)


def test_lock_is_released_when_the_block_raises(database: Path) -> None:
    with pytest.raises(RuntimeError):
        with vault_lock(database):
            raise RuntimeError("boom")
    assert not is_locked(database)
    with vault_lock(database, timeout=0):
        pass


def test_nested_acquisition_in_one_thread_is_reentrant(database: Path) -> None:
    """Application code nests: apply() runs inside a caller's critical section."""
    with vault_lock(database):
        with vault_lock(database):
            assert is_locked(database)
        assert is_locked(database)
    assert not is_locked(database)


def test_another_thread_in_the_same_process_is_excluded(database: Path) -> None:
    """flock is per descriptor, so a second thread would block on itself."""
    observed: list[str] = []

    def attempt() -> None:
        try:
            with vault_lock(database, operation="second", timeout=0.1):
                observed.append("acquired")
        except LockBusyError:
            observed.append("busy")

    with vault_lock(database, operation="first"):
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join()

    assert observed == ["busy"]


def test_second_thread_proceeds_once_the_first_releases(database: Path) -> None:
    observed: list[str] = []

    def attempt() -> None:
        with vault_lock(database, operation="second", timeout=5):
            observed.append("acquired")

    with vault_lock(database, operation="first"):
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=0.2)
        assert observed == [], "the second thread must not enter early"

    thread.join(timeout=5)
    assert observed == ["acquired"]


def test_busy_error_names_the_blocked_operation(database: Path) -> None:
    """The message has to say what could not start and where it waited."""
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with vault_lock(database, operation="index rebuild"):
            holding.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    holding.wait(timeout=5)
    try:
        with pytest.raises(LockBusyError) as failure:
            with vault_lock(database, operation="note update", timeout=0.05):
                pass
    finally:
        release.set()
        thread.join(timeout=5)

    message = str(failure.value)
    assert "note update" in message
    assert "index.db.lock" in message


def test_symlinked_lock_file_is_rejected(database: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    database.parent.mkdir(parents=True, exist_ok=True)
    lock_path(database).symlink_to(target)
    with pytest.raises(locks.LockUnsafeError):
        with vault_lock(database):
            pass


def test_registry_does_not_grow_when_only_probing(database: Path) -> None:
    """``is_locked`` is a query; it must not allocate bookkeeping."""
    before = len(locks._registry)
    is_locked(database)
    assert len(locks._registry) == before
