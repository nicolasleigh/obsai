"""What the HTTP handlers are given.

Everything a route needs arrives through this module, so no route reaches for a
global, reads configuration itself, or decides where the index lives. That is the
same rule the CLI follows after phase A, applied to the other adapter.

Three choices are worth stating:

* **Configuration is re-read per request.** Editing ``config.toml`` while the UI is
  open takes effect on the next request instead of requiring a restart. Parsing it
  costs well under a millisecond, so caching a value the user may be editing buys
  nothing. FastAPI caches the result *within* one request, so a handler that needs
  settings twice still pays once.
* **The index arrives as a handle, not a connection.** See
  :mod:`obsai.application.index` for why the connection cannot outlive the request.
* **The job runner does not.** A job is meant to outlive the request that started it
  and a client is meant to keep watching across a reload, so the runner is
  process-wide, lives on ``app.state`` and is closed by the lifespan. Which runner
  is resolved per request through :class:`JobRegistry`, so editing the configuration
  changes the answer instead of being ignored until the next restart.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from fastapi import Depends, Request

from obsai.application.index import IndexHandle, open_index
from obsai.application.jobs import JobRunner
from obsai.application.locks import is_locked, LockBusyError, vault_lock
from obsai.application.paths import database_path
from obsai.config.loader import load_settings
from obsai.config.models import Settings


@dataclass(frozen=True)
class AppState:
    """Process facts a handler cannot derive from configuration.

    ``started_at`` is what makes the lifespan observable: without it there would be
    no way to tell a freshly restarted server from one that has been running since
    before the user edited their configuration.

    Deliberately a value with no resources on it. It is spread into the ``/status``
    response, and something that owns threads and file handles does not belong in a
    payload. The job registry sits beside it on ``app.state`` instead.
    """

    started_at: datetime

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()


class JobRegistry:
    """One job runner per index, built the first time a request asks for one.

    Keyed by the index rather than created once, because configuration is re-read
    per request: pointing ``index.database`` at another index has to start answering
    about *that* index's jobs, not silently keep reporting the previous one's.
    Keying also keeps a job started against the old index resolvable — it stays in
    the runner that owns it until the process ends.

    Nothing is built at import time or at startup. A runner is created on demand and
    opens its journal even later, so a server that is only ever asked questions
    still writes nothing.
    """

    def __init__(self) -> None:
        self._runners: dict[Path, JobRunner] = {}
        self._lock = threading.Lock()

    def runner_for(self, database_path: Path) -> JobRunner:
        with self._lock:
            runner = self._runners.get(database_path)
            if runner is None:
                runner = JobRunner(database_path=database_path)
                self._runners[database_path] = runner
            return runner

    def close(self) -> None:
        """Stop every runner.

        ``JobRunner.close`` cancels live jobs cooperatively and then waits for them,
        so a job that is mid-file-transaction still gets to roll back rather than
        being abandoned with the process.
        """
        with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for runner in runners:
            runner.close()


def get_app_state(request: Request) -> AppState:
    return request.app.state.obsai


def get_job_registry(request: Request) -> JobRegistry:
    """The process's job runners. Put there by the lifespan, never built here."""
    return request.app.state.jobs


def get_settings() -> Settings:
    """The current configuration, or a ``ConfigError`` the adapter maps to 400."""
    return load_settings()


def index_handle(settings: Settings = Depends(get_settings)) -> Iterator[IndexHandle]:
    """Yield an open index, and close it when the request ends."""
    handle = open_index(settings)
    try:
        yield handle
    finally:
        handle.close()


def get_job_runner(
    settings: Settings = Depends(get_settings),
    registry: JobRegistry = Depends(get_job_registry),
) -> JobRunner:
    """The runner for the index this request is configured against.

    Not request-scoped, unlike :func:`index_handle`: a job has to outlive the request
    that started it, and a stream has to keep reading after its own handler has
    returned. The registry resolves *which* runner from the current configuration;
    the lifespan owns its lifetime.
    """
    return registry.runner_for(database_path(settings))


def get_write_lock(
    settings: Settings = Depends(get_settings),
) -> Callable[[str], AbstractContextManager[None]]:
    """A factory for the cross-process Vault lock, keyed by the operation name.

    A factory rather than a lock value because the lock is named by what it guards
    ("note update", "index rebuild") and that name is chosen by the route, not by
    the dependency graph. The lock file sits beside the index rather than inside
    the Vault — taking a lock must never modify the source of truth, and a lock file
    in a git-synced Vault would be committed.
    """

    def acquire(operation: str) -> AbstractContextManager[None]:
        return vault_lock(database_path(settings), operation=operation)

    return acquire


def check_write_lock(settings: Settings = Depends(get_settings)) -> None:
    """Refuse mutating requests if another process is currently holding the write lock.

    Checks early so that if a CLI process or another background operation is already
    holding the lock, the route immediately returns HTTP 423 Locked rather than queueing
    a doomed job that would fail later.
    """
    db_path = database_path(settings)
    if is_locked(db_path):
        raise LockBusyError(
            "Cannot start operation: another ObsAgent operation is already writing to this Vault"
        )

