"""Bounded background jobs with cooperative cancellation and a durable journal.

Three ideas carry this module.

**Cancellation reuses an existing boundary.** ``obsai.shutdown`` already holds a
controller in a :class:`~contextvars.ContextVar` and exposes ``check_shutdown()``,
which the embedding pipeline, the agent workflow and the indexer already call at
their safe points. Installing a :class:`CancellationToken` for the duration of a
job therefore makes all of them cancellable without editing any of them.

**Cancellation is not shutdown.** ``ShutdownRequested`` means the process is
going away and carries an exit code; a cancelled job leaves the process alive and
must be distinguishable. :class:`JobCancelled` is a separate ``BaseException`` —
``BaseException`` so that the broad ``except Exception`` guards inside the
domain layer cannot swallow it, and separate so callers can tell the two apart.
Domain code that catches ``BaseException`` in order to roll back should consult
:func:`obsai.shutdown.is_process_stop`, which recognises both.

**A job outlives the process that ran it.** :class:`JobStore` writes the shape of
each job — kind, status, timestamps, scalar progress metrics, error code — into a
journal beside the index, so that a restart can report what happened instead of
forgetting it. The journal is deliberately *not* a table in the index database:
``obsai index rebuild`` builds a fresh index and ``os.replace``s it over the live
one, which would empty a job table on every rebuild, and a job record is not
derivable from the Vault the way the index is. Jobs still running when a process
dies are marked ``interrupted`` — not ``succeeded``, which would be a lie, and not
``cancelled``, which would blame the user for a crash. The journal is opened **on
demand**, and only a write creates it: a runner that is merely asked about its jobs
has to answer "there are none" without making the file that turns that answer into
a lie.

The runner is single-worker by default because every job it is expected to run
mutates either the Vault or the index; serialising them is cheaper than
reasoning about interleaved writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from obsai.application.dto import JobStatus, JobView
from obsai.errors import NotFoundError, SchemaError
from obsai.shutdown import current_controller, install_controller
from obsai.storage import Database

# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #

#: Appended to the index file name, so two Vaults with separate indexes do not
#: read each other's jobs: ``index.db`` gets ``index.jobs.db``.
JOBS_SUFFIX = ".jobs.db"

JOBS_SCHEMA_VERSION = 1

#: A progress entry is a metric, not a payload. A longer string is dropped rather
#: than truncated — half a note is still note text.
MAX_SUMMARY_TEXT = 500
MAX_SUMMARY_KEYS = 32
MAX_ERROR_TEXT = 1000

#: Progress is published per batch and every journal write is its own transaction,
#: so an unthrottled journal turns a long index run into a stream of fsyncs.
#: Status changes are never throttled.
PERSIST_INTERVAL_SECONDS = 1.0

INTERRUPTED_MESSAGE = "Interrupted: the process exited while this job was running"

JOBS_SCHEMA_V1 = (
    """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        message TEXT,
        summary_json TEXT NOT NULL,
        error_code TEXT,
        error TEXT
    )""",
    "CREATE INDEX idx_jobs_created ON jobs(created_at)",
)


def initialize_jobs_schema(connection: sqlite3.Connection) -> None:
    """Create the journal, or refuse a database that is something else.

    The unversioned-and-non-empty guard is the same one the index schema uses, and
    it is what stops a mis-pointed journal path from writing job rows into an
    index: an index carries ``user_version = 3`` and is refused by version.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == JOBS_SCHEMA_VERSION:
        return
    if version != 0:
        raise SchemaError(f"Unsupported job journal schema version: {version}")
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    if existing is not None:
        raise SchemaError("Unversioned job journal is not empty")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in JOBS_SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def job_journal_path(database_path: Path) -> Path:
    return database_path.with_suffix(JOBS_SUFFIX)


def open_job_journal(database_path: Path) -> "JobStore | None":
    """Open the journal for an index, or ``None`` when no job has ever run.

    Opening the journal creates it, and recovery runs at startup on paths that are
    supposed to touch nothing — including the read-only ones. A process has to be
    able to ask "were there jobs?" without answering "yes" by asking.
    """
    path = job_journal_path(database_path)
    if not path.exists():
        return None
    return JobStore(path)


def _format_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _summary(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep scalar progress metrics; drop everything else.

    ``detail`` is whatever a job callable passed to ``progress()``. Filtering here,
    rather than trusting callers, is what makes "the journal never holds note text"
    a property of the shape instead of a rule somebody has to remember.
    """
    summary: dict[str, Any] = {}
    for key in sorted(detail):
        value = detail[key]
        if value is None or isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str) and len(value) <= MAX_SUMMARY_TEXT:
            summary[key] = value
        if len(summary) == MAX_SUMMARY_KEYS:
            break
    return summary


def _view_from_row(row: sqlite3.Row) -> JobView:
    return JobView(
        job_id=row["job_id"],
        kind=row["kind"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_time(row["started_at"]),
        finished_at=_parse_time(row["finished_at"]),
        message=row["message"],
        detail=json.loads(row["summary_json"]),
        error_code=row["error_code"],
        error=row["error"],
    )


class JobStore:
    """The durable journal that lives beside one index.

    Written by the worker thread running a job and read by the thread serving a
    request, so the connection is opened with ``check_same_thread=False`` and every
    operation holds a lock. Sharing one connection without that lock would let a
    reader see another thread's open transaction and would interleave two
    ``BEGIN IMMEDIATE`` blocks that each believe they are the outer one.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._database = Database(path, schema=initialize_jobs_schema, check_same_thread=False)
        self._lock = threading.Lock()

    def save(self, view: JobView) -> None:
        with self._lock, self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs (job_id, kind, status, created_at, started_at,
                                  finished_at, message, summary_json, error_code, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    message = excluded.message,
                    summary_json = excluded.summary_json,
                    error_code = excluded.error_code,
                    error = excluded.error
                """,
                (
                    view.job_id,
                    view.kind,
                    view.status,
                    _format_time(view.created_at),
                    _format_time(view.started_at),
                    _format_time(view.finished_at),
                    view.message,
                    json.dumps(_summary(view.detail), ensure_ascii=False, sort_keys=True),
                    view.error_code,
                    _truncate(view.error, MAX_ERROR_TEXT),
                ),
            )

    def load(self, job_id: str) -> JobView:
        with self._lock:
            row = self._database.connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Unknown job: {job_id}")
        return _view_from_row(row)

    def list(self, *, limit: int = 200) -> list[JobView]:
        with self._lock:
            rows = self._database.connection.execute(
                "SELECT * FROM jobs ORDER BY created_at, rowid LIMIT ?", (limit,)
            ).fetchall()
        return [_view_from_row(row) for row in rows]

    def interrupt_unfinished(self, message: str) -> int:
        """Close out jobs a dead process left open, and report how many.

        ``cancelled`` would blame the user and ``succeeded`` would be false.
        ``interrupted`` is the only status that says what is actually known: the job
        stopped, and nothing this side of the crash can tell how far it got.
        """
        with self._lock, self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                   SET status = 'interrupted', finished_at = ?, message = ?
                 WHERE status IN ('queued', 'running', 'awaiting_approval')
                """,
                (_format_time(datetime.now(timezone.utc)), message),
            )
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._database.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


class JobCancelled(BaseException):
    """Raised at a cooperative checkpoint when a job is cancelled by the user.

    Deliberately not a subclass of :class:`obsai.errors.ObsAIError`: cancellation
    is not a failure, and the CLI boundary must not render it as one.
    """

    def __init__(self, reason: str = "Job cancelled"):
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """A per-job cancellation flag that plugs into ``check_shutdown()``.

    Also forwards to the ambient controller (normally the process-wide
    ``ShutdownController``), so Ctrl-C still interrupts a running job.
    """

    def __init__(self, previous: Any = None) -> None:
        self._cancelled = threading.Event()
        self._reason: str | None = None
        self._previous = previous
        self._defer_depth = 0

    def cancel(self, reason: str | None = None) -> None:
        self._reason = reason
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        """Called at safe boundaries via ``check_shutdown()``."""
        if self._defer_depth == 0 and self._cancelled.is_set():
            raise JobCancelled(self._reason or "Job cancelled")
        if self._previous is not None:
            self._previous.check()

    @contextmanager
    def defer(self) -> Iterator[None]:
        """Hold cancellation off for the duration of one atomic filesystem unit.

        ``check_shutdown``'s companion ``defer_shutdown`` calls this on whatever
        controller is installed, so a token that did not implement it — or did not
        forward it — would let a signal fire in the middle of a file commit, which
        is the exact window ``defer_shutdown`` exists to protect.
        """
        self._defer_depth += 1
        try:
            forward = getattr(self._previous, "defer", None)
            if forward is None:
                yield
            else:
                with forward():
                    yield
        finally:
            self._defer_depth -= 1

    @contextmanager
    def installed(self) -> Iterator[None]:
        with install_controller(self):
            yield


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class _Record:
    """Mutable bookkeeping for one job; converted to a frozen view on read."""

    job_id: str
    kind: str
    status: JobStatus = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    token: CancellationToken = field(default_factory=CancellationToken)
    approval_request: dict[str, Any] | None = None
    approval_decision: bool | None = None
    approval_event: threading.Event = field(default_factory=threading.Event)

    def view(self) -> JobView:
        return JobView(
            job_id=self.job_id,
            kind=self.kind,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            message=self.message,
            detail=dict(self.detail),
            error=self.error,
            error_code=self.error_code,
        )


class _Journal:
    """Writes one job's record through to the store, throttled unless forced.

    A bound callable rather than a method on the record, so that a job callable
    cannot reach the journal except through :class:`JobContext`.
    """

    def __init__(self, store: JobStore | None, record: _Record) -> None:
        self._store = store
        self._record = record
        self._written_at = 0.0

    def __call__(self, *, force: bool = False) -> None:
        store = self._store
        if store is None:
            return
        now = time.monotonic()
        if not force:
            if now - self._written_at < PERSIST_INTERVAL_SECONDS:
                return
            # Only a throttled write opens the next interval. Letting a status
            # change reset the clock would drop the progress report immediately
            # after it — the one a job that is about to get stuck is most likely
            # to be sitting on.
            self._written_at = now
        try:
            store.save(self._record.view())
        except Exception as exc:  # noqa: BLE001 - the journal is not the job
            # A failed journal write must not turn finished work into a reported
            # failure, so the job keeps its status and the failure is published in
            # the record instead of being swallowed.
            self._record.detail["journal_error"] = f"{type(exc).__name__}: {exc}"


class JobContext:
    """Handle passed to job callables: progress, approval, cancellation."""

    def __init__(self, record: _Record, journal: _Journal) -> None:
        self._record = record
        self._journal = journal

    @property
    def job_id(self) -> str:
        return self._record.job_id

    @property
    def token(self) -> CancellationToken:
        return self._record.token

    def progress(self, message: str, **detail: Any) -> None:
        """Publish a human-readable step. Also a cancellation checkpoint."""
        self._record.message = message
        self._record.detail.update(detail)
        self._journal()
        self.token.check()

    def await_approval(self, request: dict[str, Any]) -> bool:
        """Block until a human decides, reporting ``awaiting_approval`` meanwhile.

        The decision is delivered by :meth:`JobRunner.resolve_approval`. A job
        cancelled while waiting wakes up and raises rather than hanging forever,
        and cancellation wins over a decision that raced it: the fail-safe answer
        to "was this approved?" is no.

        The status change is journaled immediately: a process that dies while a job
        waits for a human leaves a record saying so, which is the one case where
        ``interrupted`` and ``running`` mean visibly different things.
        """
        record = self._record
        record.approval_request = dict(request)
        record.approval_decision = None
        record.approval_event.clear()
        record.status = "awaiting_approval"
        self._journal(force=True)
        try:
            while not record.approval_event.wait(timeout=0.1):
                record.token.check()
            # ``cancel`` also sets the event, so a cancellation that woke the wait
            # would otherwise be read as a decision.
            record.token.check()
        finally:
            record.approval_request = None
            record.status = "running"
        return bool(record.approval_decision)


JobCallable = Callable[[JobContext], dict[str, Any] | None]


class JobNotFoundError(NotFoundError):
    """No job with this id is known — not live here, and not in the journal.

    A subclass of :class:`~obsai.errors.NotFoundError` rather than a bare one so
    that the HTTP adapter can answer ``job_not_found``. The status code still comes
    from the parent through the MRO (see :mod:`obsai.api.errors`), but
    ``web/src/lib/errors.ts`` maps *codes* to sentences, and "this note is not in
    the index" is the wrong sentence for a job that never existed.
    """


class JobRunner:
    """In-process, bounded job runner.

    ``max_workers=1`` is the default and the intended setting: it makes write
    jobs mutually exclusive without a separate lock. Cross-process exclusion is
    :mod:`obsai.application.locks`'s job, not this one's.

    Three ways to say where the journal is, and the difference matters:

    * ``store`` — an already-open journal, used as given for both reading and
      writing. This is what a caller that owns a store passes.
    * ``database_path`` — the index these jobs belong to; the journal is
      ``<database_path>.jobs.db``, opened the first time it is needed. A read opens
      it only if it exists; a write creates it.
    * neither — an in-process runner with no journal at all. The CLI wants this: a
      command that runs one job and exits has nothing to persist.

    ``store=None`` alone cannot express the second case, because it is
    indistinguishable from "the journal is simply not there yet" — which is why
    ``database_path`` exists instead of leaving the API to guess.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        store: JobStore | None = None,
        database_path: Path | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="obsai-job")
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._store = store
        self._database_path = database_path

    def _journal_store(self, *, create: bool) -> JobStore | None:
        """The journal, opened at most once, and only if ``create`` allows it.

        Opening a journal creates it, so a *read* must not do it: the routes that
        report on jobs run on machines whose index has never seen one, and B-9's
        read-only guarantee is that asking a question does not change the answer.
        Reads therefore take the :func:`open_job_journal` path, which refuses to
        create; only a write creates.

        A failed lookup is not remembered — ``self._store`` stays ``None`` and the
        next caller looks again, so a journal that appears later is still found.
        """
        if self._store is not None:
            return self._store
        if self._database_path is None:
            return None
        self._store = (
            JobStore(job_journal_path(self._database_path))
            if create
            else open_job_journal(self._database_path)
        )
        return self._store

    def submit(self, kind: str, work: JobCallable) -> JobView:
        """Queue ``work`` and return its initial view."""
        if self._closed:
            raise RuntimeError("JobRunner is closed")
        record = _Record(job_id=uuid4().hex, kind=kind)
        with self._lock:
            self._records[record.job_id] = record
        try:
            store = self._journal_store(create=True)
            if store is not None:
                # Strict, unlike every later write: a job that cannot be journaled
                # is not started at all, so no caller is ever handed an id that the
                # journal has never seen and a restart could not resolve. The
                # *open* is inside the guard as well as the write, because creating
                # the journal is itself a step that can fail.
                store.save(record.view())
        except Exception:
            # Nothing is running and nothing will be, so the bookkeeping goes with
            # it: a refused submission must not leave behind a ``queued`` job that
            # no worker will ever pick up.
            with self._lock:
                self._records.pop(record.job_id, None)
            raise
        self._executor.submit(self._run, record, work)
        return record.view()

    def _run(self, record: _Record, work: JobCallable) -> None:
        # ``create=True`` is normally a no-op here, because ``submit`` already
        # created the journal before it would start anything. It matters for the
        # case where a read opened one in between.
        journal = _Journal(self._journal_store(create=True), record)
        record.status = "running"
        record.started_at = datetime.now(timezone.utc)
        journal(force=True)
        # Inherit the ambient controller so Ctrl-C keeps working inside the job.
        record.token._previous = current_controller()
        status: JobStatus = "succeeded"
        message: str | None = None
        error: str | None = None
        error_code: str | None = None
        detail: dict[str, Any] = {}
        try:
            with record.token.installed():
                record.token.check()
                detail = work(JobContext(record, journal)) or {}
        except JobCancelled as exc:
            status, message = "cancelled", exc.reason
        except BaseException as exc:  # noqa: BLE001 - a job boundary must not leak
            status = "failed"
            error_code = type(exc).__name__
            error = f"{error_code}: {exc}"
            detail = {"traceback": traceback.format_exc()}
        # The terminal state is published in one step, and the status last: a
        # reader that polls and stops at a terminal status must not be able to
        # observe "failed" with the error and detail still missing.
        record.detail.update(detail)
        record.error_code = error_code
        record.error = error
        if message is not None:
            record.message = message
        record.finished_at = datetime.now(timezone.utc)
        record.status = status
        journal(force=True)

    def get(self, job_id: str) -> JobView:
        """Live state when this process is running the job, else the journal's.

        That fallback is what lets a job id survive a restart: the id a caller was
        given before the restart is still the id it can ask about afterwards.
        """
        with self._lock:
            record = self._records.get(job_id)
        if record is not None:
            return record.view()
        store = self._journal_store(create=False)
        if store is None:
            raise JobNotFoundError(f"Unknown job: {job_id}")
        return store.load(job_id)

    def list(self) -> list[JobView]:
        with self._lock:
            records = list(self._records.values())
        # Live records win over their journal rows, which may be up to one throttle
        # interval behind.
        merged = {record.job_id: record.view() for record in records}
        store = self._journal_store(create=False)
        if store is not None:
            for view in store.list():
                merged.setdefault(view.job_id, view)
        return sorted(merged.values(), key=lambda item: item.created_at)

    def recover(self) -> int:
        """Close out jobs a previous process left open; returns how many.

        A startup action rather than something ``__init__`` does quietly, because
        it writes, and a constructor that writes is a constructor nobody can call
        from a read-only path. It reads with ``create=False`` for the same reason:
        a machine that has never run a job has nothing to recover, and the way to
        say so is to find no journal rather than to make one.
        """
        store = self._journal_store(create=False)
        if store is None:
            return 0
        return store.interrupt_unfinished(INTERRUPTED_MESSAGE)

    def cancel(self, job_id: str, reason: str | None = None) -> bool:
        """Request cancellation. Returns False for an unknown or finished job.

        Cancellation is cooperative: the job stops at its next ``check_shutdown()``
        boundary, so a file transaction in flight still completes or rolls back.
        """
        with self._lock:
            record = self._records.get(job_id)
        if record is None or record.finished_at is not None:
            return False
        record.token.cancel(reason)
        record.approval_event.set()
        return True

    def resolve_approval(self, job_id: str, *, approved: bool) -> bool:
        """Deliver a human decision to a job blocked in ``await_approval``."""
        with self._lock:
            record = self._records.get(job_id)
        if record is None or record.approval_request is None:
            return False
        record.approval_decision = approved
        record.approval_event.set()
        return True

    def close(self, *, wait: bool = True) -> None:
        """Cancel every live job, shut the executor down, then close the journal.

        The journal stays open until the executor has finished: a cancelled job's
        terminal write happens during the shutdown, and closing underneath it would
        lose exactly the record the caller wants to see.
        """
        with self._lock:
            records = list(self._records.values())
        for record in records:
            if record.finished_at is None:
                record.token.cancel("Runner shutting down")
                record.approval_event.set()
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if self._store is not None:
            self._store.close()

    def __enter__(self) -> "JobRunner":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def recover_interrupted_jobs(database_path: Path) -> int:
    """Startup recovery for a process that has not built a runner yet.

    Returns how many jobs were still open, so the caller can report it rather than
    guess. Touches nothing when no job has ever run against this index.
    """
    store = open_job_journal(database_path)
    if store is None:
        return 0
    with store:
        return store.interrupt_unfinished(INTERRUPTED_MESSAGE)
