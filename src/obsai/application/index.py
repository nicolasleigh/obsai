"""A request-scoped handle on the derived index.

Every route needs the same three answers before it can do anything: where the index
is, whether it is open, and — if it is not — why. Resolving that once keeps the
routes to a line each, and it lets ``/status`` *describe* a broken index instead of
failing on it, which is the entire point of an overview screen.

Two decisions are load-bearing:

* **Not a process-wide connection.** SQLite connections are bound to the thread
  that created them and FastAPI runs synchronous handlers in a worker thread, so a
  connection opened in the lifespan would raise the moment a request touched it.
  The handle is therefore created and closed inside the request that uses it.
* **"Inside the request" is not "on one thread".** FastAPI submits a dependency's
  ``__enter__`` and its ``__exit__`` to the threadpool as two separate jobs, and
  anyio is free to run them on different workers — so the connection is opened with
  ``check_same_thread=False``. What makes that safe is confinement: the handle
  belongs to exactly one request, and nothing else ever touches it.
* **A missing index is not a failure here.** The index is a rebuildable artefact
  and "not built yet" is a normal state that callers must be able to render.
  :meth:`IndexHandle.require` is where that becomes an error, so only the routes
  that genuinely need data pay for it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from obsai.application.paths import MISSING_INDEX_MESSAGE, database_path
from obsai.config.models import Settings
from obsai.errors import ConfigError, ObsAIError
from obsai.storage.database import Database


@dataclass
class IndexHandle:
    """The derived index as one request sees it."""

    path: Path
    #: Whether anything occupies the index path — not whether it is usable.
    exists: bool = False
    database: Database | None = None
    failure: Exception | None = None

    @property
    def usable(self) -> bool:
        return self.database is not None

    @property
    def error(self) -> str | None:
        return None if self.failure is None else str(self.failure)

    def require(self) -> Database:
        """The open index, or the reason it could not be opened.

        A missing index raises the same ``ConfigError`` the CLI prints. An index
        that exists but cannot be read re-raises the original failure, so a corrupt
        schema stays a server-side error rather than being flattened into a 400.
        """
        if self.database is not None:
            return self.database
        if self.failure is not None:
            raise self.failure
        raise ConfigError(MISSING_INDEX_MESSAGE)

    def close(self) -> None:
        if self.database is not None:
            self.database.close()
            self.database = None


def open_index(settings: Settings) -> IndexHandle:
    """Open the index when it has been built; never raise for a missing one."""
    path = database_path(settings)
    # ``exists`` rather than ``is_file``: a path occupied by a directory is a state
    # worth reporting. Claiming "not built yet" would send the user to
    # ``index update``, a command that fails for the same reason.
    if not path.exists():
        return IndexHandle(path=path)
    try:
        # ``check_same_thread=False``: the handle is confined to one request, but
        # that request's setup and cleanup can land on different worker threads.
        # See the module docstring.
        return IndexHandle(
            path=path, exists=True, database=Database(path, check_same_thread=False)
        )
    except (OSError, sqlite3.Error, ObsAIError) as exc:
        # ``Database`` wraps most failures in ``SchemaError``, but a connection
        # that cannot be established at all surfaces as ``sqlite3.Error`` or
        # ``OSError``. Both mean the same thing to a caller: the index exists and
        # cannot be used. A ``TypeError`` from a bug in ``Database`` is deliberately
        # *not* caught — that should still be a loud 500.
        return IndexHandle(path=path, exists=True, failure=exc)
