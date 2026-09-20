"""C-2 acceptance: the progress stream, over a real socket.

**Why not ``TestClient``.** It buffers the whole response body before handing it back:
the httpx ASGI transport collects every body chunk and returns a finished response, so
``client.stream()`` on an endless stream blocks until the stream ends — which it never
does. Measured directly: a generator that yielded its second event immediately was not
visible to the client until the generator had run to completion. A test written through
that transport would either hang or prove nothing about streaming, so this module starts
a real uvicorn server on an ephemeral port and talks to it with a real HTTP client.

**What the two acceptance criteria are.** "Manually disconnect and reconnect, and the
progress continues" and "cancelling requires an explicit ``POST /jobs/{id}/cancel``".
Both are about the same thing: a stream that only reads cannot stop anything, and state
that lives in a journal does not need to be replayed to be resumed. So the test below
disconnects mid-job, asserts the job kept running and moved forward, reconnects, asserts
the snapshot reports where it actually is, and only then cancels it with a request.

**The counterweight.** Watching a job must not create the thing being watched. The
second test opens the stream, reads its snapshot and asks for a job that does not exist
on an index that has never run one, and asserts ``index.jobs.db`` was never created —
B-9's read-only guarantee, restated for the heaviest read path in the project.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn

from obsai.api.app import create_app
from obsai.application.jobs import job_journal_path
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

#: Read is generous but finite: a stalled stream raises instead of hanging the suite.
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

#: Bounded so a leftover connection cannot hold the server open forever. uvicorn's
#: default is to wait indefinitely, which is right in production and wrong in a test.
GRACEFUL_SHUTDOWN_SECONDS = 5


class Stepwise:
    """A job that reports a step number until it is stopped or cancelled.

    Bounded by an event rather than by a step count so the test can end it at
    teardown no matter which branch the assertions took. ``progress`` is also the
    cancellation checkpoint, so a cancelled run leaves through ``JobCancelled``
    rather than through ``stop``.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.steps = 0

    def __call__(self, context) -> dict[str, int]:
        while not self.stop.is_set():
            self.steps += 1
            context.progress("step", step=self.steps)
            time.sleep(0.01)
        return {"steps": self.steps}


def build(tmp_path: Path) -> Path:
    """A one-note Vault with a built index, and a config that points at both."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )
    return database_path


@contextmanager
def running_server(app) -> Iterator[str]:
    """A real server on an ephemeral port, in this process.

    In-process rather than a subprocess so the test can reach the runner the server is
    using and submit a job to it: C-2 has no endpoint that starts one (that is C-3), and
    the alternative — testing the stream against a job that does not exist — would leave
    the acceptance untested.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn never finished starting"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        # Streams must be closed by the caller before this: setting ``should_exit``
        # directly does not go through uvicorn's signal handler, so sse-starlette never
        # learns to stop and the server would wait out its grace period.
        server.should_exit = True
        thread.join(timeout=15 + GRACEFUL_SHUTDOWN_SECONDS)
        assert not thread.is_alive(), "uvicorn did not shut down"


def read_events(response: httpx.Response, *, count: int) -> list[tuple[str, str]]:
    """Read ``count`` SSE events off a streaming response as ``(name, data)``.

    Skips the keep-alive comments sse-starlette sends every 15 seconds; they carry no
    ``event:`` line, so they fall out of the accumulator on their own.
    """
    events: list[tuple[str, str]] = []
    name: str | None = None
    data: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if name is not None:
                events.append((name, "\n".join(data)))
                if len(events) == count:
                    return events
            name, data = None, []
        elif line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
    raise AssertionError(f"stream ended after {len(events)} of {count} events")


def snapshot_of(base: str) -> list[dict]:
    """The first thing every connection sends: every job, as of now."""
    with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as client:
        with client.stream("GET", f"{base}/api/v1/events") as response:
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            events = read_events(response, count=1)
    name, data = events[0]
    assert name == "snapshot", f"第一条事件不是 snapshot，而是 {name}"
    return json.loads(data)


def wait_until(predicate, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


# --------------------------------------------------------------------------- #
# The acceptance
# --------------------------------------------------------------------------- #


def test_a_disconnect_neither_cancels_nor_loses_progress(tmp_path: Path) -> None:
    database_path = build(tmp_path)
    job = Stepwise()
    app = create_app()

    with running_server(app) as base:
        runner = app.state.jobs.runner_for(database_path)
        submitted = runner.submit("probe", job)
        try:
            # --- connect: the snapshot says what is running ---------------------
            first = snapshot_of(base)
            assert [item["job_id"] for item in first] == [submitted.job_id]
            seen = first[0]["detail"].get("step", 0)

            # --- disconnect: the job must not notice ---------------------------
            wait_until(lambda: runner.get(submitted.job_id).detail.get("step", 0) > seen + 3)
            assert runner.get(submitted.job_id).status == "running", (
                "断开连接把任务取消了——断线不等于取消"
            )

            # --- reconnect: progress continues, it does not restart -------------
            resumed = snapshot_of(base)
            assert [item["job_id"] for item in resumed] == [submitted.job_id]
            assert resumed[0]["status"] == "running"
            assert resumed[0]["detail"]["step"] > seen, (
                "重连之后进度没有往前——快照没有反映当前状态"
            )

            # --- cancel: only an explicit request stops it ----------------------
            with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as client:
                response = client.post(f"{base}/api/v1/jobs/{submitted.job_id}/cancel")
                assert response.status_code == 200, response.text
                assert response.json()["job_id"] == submitted.job_id

            wait_until(lambda: runner.get(submitted.job_id).status == "cancelled")
            with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as client:
                final = client.get(f"{base}/api/v1/jobs/{submitted.job_id}").json()
            assert final["status"] == "cancelled"
            assert final["finished_at"] is not None
        finally:
            job.stop.set()


def test_watching_jobs_creates_no_journal(tmp_path: Path) -> None:
    """The read-only guarantee, on the read path that runs twice a second."""
    database_path = build(tmp_path)
    app = create_app()

    with running_server(app) as base:
        assert snapshot_of(base) == []

        with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as client:
            assert client.get(f"{base}/api/v1/jobs").json() == []
            missing = client.get(f"{base}/api/v1/jobs/nope")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "job_not_found"

    assert not job_journal_path(database_path).exists(), (
        "只看了一眼任务，就在索引旁边留下了一个任务日志"
    )


def test_a_job_id_survives_a_restart(tmp_path: Path) -> None:
    """The other half of "reload and resume": the id outlives the process."""
    database_path = build(tmp_path)
    app = create_app()

    with running_server(app) as base:
        runner = app.state.jobs.runner_for(database_path)
        submitted = runner.submit("probe", lambda context: {"notes": 2})
        wait_until(lambda: runner.get(submitted.job_id).terminal)

    # A second server, so a second lifespan and a second runner: the first one's
    # records are gone and only the journal is left to answer from.
    with running_server(create_app()) as base:
        with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as client:
            response = client.get(f"{base}/api/v1/jobs/{submitted.job_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded"
        assert snapshot_of(base)[0]["job_id"] == submitted.job_id
