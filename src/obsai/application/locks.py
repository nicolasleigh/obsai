"""Cross-process mutual exclusion for Vault mutations and derived-index writes.

Four classes of operation mutate state that another process can also be reading
or writing:

* Vault file transactions — single-note ``SafeWriteService`` commits and
  multi-file ``TransactionService`` commits
* incremental index updates — ``obsai index update``
* embedding writes — ``obsai index embeddings``
* shadow index rebuilds — ``obsai index rebuild``

They are serialised by one advisory ``fcntl.flock`` on a single lock file.
Advisory is the right level here: every writer in this project goes through the
same code, so a cooperative protocol is enough, and an advisory lock has a
property a lock *file* with a pid inside does not — the kernel drops it when the
descriptor closes, so a crashed or ``kill -9``'d process can never leave the
Vault permanently wedged.

**Reads never lock.** ``search``, ``ask``, ``links``, ``transaction status`` and
``note`` previews all run against a consistent snapshot and are expected to stay
concurrent with a long ``index update``.

**Where the lock file lives.** Beside the index database — ``index.db`` gets
``index.db.lock`` — mirroring the existing shadow-rebuild lock
(``index.db.building.lock``). The alternative, a file inside the Vault, was
rejected: acquiring a lock must never mutate the source of truth, and Vaults are
frequently git-synchronised, so a stray lock file would show up as a commit.
The lock is keyed on the database rather than the Vault root because the database
path is the one state anchor every mutating command already derives from
configuration; two configurations pointing at the same Vault but different index
files would not be serialised, which is noted as a known limitation rather than
hidden.

**Re-entrancy.** ``flock`` is per open file description, so a second ``open`` +
``flock`` in the same process blocks just like another process would. Application
code nests (``organizer.apply`` calls into a transaction commit; ``links suggest
--apply`` plans and then executes), so the context manager is re-entrant per
thread and mutually exclusive across threads in the same process.

**Interaction with the shadow-rebuild lock.** ``ShadowIndexRebuilder`` takes its
own ``.building.lock`` to stop two rebuilds racing each other. That lock knows
nothing about note writes or incremental updates, so it does **not** replace this
one; a rebuild acquires both.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from obsai.errors import ObsAIError
from obsai.shutdown import check_shutdown

LOCK_SUFFIX = ".lock"

DEFAULT_TIMEOUT = 15.0
"""Seconds to wait for a contended lock before giving up.

Long enough to absorb a short note write racing a rebuild, short enough that a
stuck peer surfaces as an error instead of an apparent hang. ``None`` waits
indefinitely, ``0`` fails immediately.
"""

_POLL_INTERVAL = 0.05


class LockBusyError(ObsAIError):
    """Another process holds the Vault write lock."""


class LockUnsafeError(ObsAIError):
    """The lock file is a symlink or otherwise cannot be trusted."""


@dataclass
class _Held:
    """Per-lock-file state shared by every thread in this process."""

    gate: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    descriptor: int | None = None


_registry: dict[str, _Held] = {}
_registry_guard = threading.Lock()


def lock_path(database_path: Path) -> Path:
    """The lock file guarding ``database_path``.

    ``realpath`` is applied so that ``/tmp/...`` and ``/private/tmp/...`` — the
    same file on macOS — produce the same key. Otherwise one thread could hold
    two descriptors on one inode and block on itself.
    """
    resolved = Path(os.path.realpath(database_path.expanduser()))
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


def _held_for(path: Path) -> _Held:
    key = str(path)
    with _registry_guard:
        held = _registry.get(key)
        if held is None:
            held = _Held()
            _registry[key] = held
        return held


def _open_lock_file(path: Path) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LockUnsafeError(f"Cannot create the lock directory {path.parent}: {exc}") from exc
    if path.is_symlink():
        raise LockUnsafeError(f"Unsafe lock file: {path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise LockUnsafeError(f"Cannot open the lock file {path}: {exc}") from exc


def _acquire_file_lock(held: _Held, path: Path, *, operation: str, timeout: float | None) -> None:
    descriptor = _open_lock_file(path)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockBusyError(
                        f"Cannot start {operation}: another ObsAgent operation is already "
                        f"writing to this Vault (waited {timeout:g}s for {path})"
                    ) from None
                # A deferred signal only sets a flag; polling keeps Ctrl-C able
                # to break a long wait instead of sleeping through it.
                check_shutdown()
                time.sleep(_POLL_INTERVAL)
    except BaseException:
        os.close(descriptor)
        raise
    held.descriptor = descriptor


def _release_file_lock(held: _Held) -> None:
    descriptor, held.descriptor = held.descriptor, None
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def vault_lock(
    database_path: Path,
    *,
    operation: str = "write",
    timeout: float | None = DEFAULT_TIMEOUT,
) -> Iterator[None]:
    """Hold the Vault write lock for the duration of the block.

    ``operation`` only labels the error message ("another ObsAgent *index
    rebuild* is already writing"), so a user can tell what they collided with.
    """
    path = lock_path(database_path)
    held = _held_for(path)
    # A negative timeout other than -1 is rejected by threading, so ``None``
    # ("wait forever") has to be translated rather than passed through.
    wait = -1.0 if timeout is None else timeout
    if not held.gate.acquire(timeout=wait):
        raise LockBusyError(
            f"Cannot start {operation}: another ObsAgent operation is already writing to "
            f"this Vault (waited {timeout:g}s for {path})"
        )
    try:
        if held.depth == 0:
            _acquire_file_lock(held, path, operation=operation, timeout=timeout)
        held.depth += 1
    except BaseException:
        held.gate.release()
        raise
    try:
        yield
    finally:
        held.depth -= 1
        if held.depth == 0:
            _release_file_lock(held)
        held.gate.release()


def is_locked(database_path: Path) -> bool:
    """Whether any process currently holds the lock.

    Diagnostic only — the answer can be stale the moment it is returned, so no
    decision may depend on it. Used by the UI to explain why a job is waiting.
    """
    path = lock_path(database_path)
    with _registry_guard:
        held = _registry.get(str(path))
    if held is not None and held.depth:
        return True
    if not path.exists():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)
