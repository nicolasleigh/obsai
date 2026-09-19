"""C-1 acceptance, end to end: a job that was running when the process died.

``test_application_job_journal.py`` proves the recovery *rule* — a row left in
``running`` becomes ``interrupted``. This module proves it happens to a real
process, because a rule only ever exercised by writing the row it is supposed to
repair has never actually seen a crash.

The child is killed with ``SIGKILL`` on purpose: ``SIGTERM`` would let the shutdown
controller cancel the job cooperatively, the job would journal ``cancelled``, and
the test would pass for the wrong reason. ``SIGKILL`` leaves exactly what a power
cut leaves — a journal saying ``running`` with nothing behind it.

The second test is the reason the journal is not a table in the index:
``obsai index rebuild`` builds a fresh index and ``os.replace``s it over the live
one, so a job table inside it would be emptied by every rebuild.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from obsai.application.dto import JobView
from obsai.application.jobs import JobStore, job_journal_path, recover_interrupted_jobs
from obsai.cli.app import app

runner = CliRunner()

CHILD = """
import json, sys, time
from pathlib import Path
from obsai.application.jobs import JobRunner, JobStore

def work(context):
    context.progress("Embedding batch 1", notes=1)
    while True:
        time.sleep(0.05)

with JobRunner(store=JobStore(Path(sys.argv[1]))) as jobs:
    print(json.dumps({"job_id": jobs.submit("embedding", work).job_id}), flush=True)
    time.sleep(60)
"""


def read_job(path: Path, job_id: str) -> tuple[str, dict] | None:
    """Read one journal row without disturbing the writer.

    ``timeout`` is the SQLite busy timeout: a read that lands while the child is
    committing its progress would otherwise raise ``database is locked`` and make
    this test fail for a reason that has nothing to do with recovery.
    """
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    try:
        row = connection.execute(
            "SELECT status, summary_json FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else (row[0], json.loads(row[1]))


def test_a_job_running_when_the_process_is_killed_is_interrupted(tmp_path: Path) -> None:
    journal = job_journal_path(tmp_path / "index.db")
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(journal)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        line = child.stdout.readline()
        if not line:
            raise AssertionError(f"the child produced no job id: {child.stderr.read()}")
        job_id = json.loads(line)["job_id"]

        deadline = time.monotonic() + 30.0
        while True:
            row = read_job(journal, job_id)
            # Waiting for ``running`` alone would race the job's own progress
            # write, and the kill could land before there was anything to lose.
            if row is not None and row[0] == "running" and row[1].get("notes") == 1:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"the child never journaled its progress: {row}")
            time.sleep(0.05)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)

    assert child.returncode == -signal.SIGKILL, "the child was not killed outright"
    assert (read_job(journal, job_id) or ("", {}))[0] == "running", "the crash left no open job"

    assert recover_interrupted_jobs(tmp_path / "index.db") == 1

    with JobStore(journal) as store:
        view = store.load(job_id)
    assert view.status == "interrupted"
    assert view.terminal
    assert view.finished_at is not None
    assert view.message is not None and "Interrupted" in view.message
    assert view.detail["notes"] == 1, "the progress written before the crash was lost"


def test_an_index_rebuild_does_not_clear_the_journal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )

    assert runner.invoke(app, ["index", "update"]).exit_code == 0

    journal = job_journal_path(database_path)
    with JobStore(journal) as store:
        store.save(
            JobView(
                job_id="j1",
                kind="index-update",
                status="succeeded",
                created_at=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
            )
        )

    rebuilt = runner.invoke(app, ["index", "rebuild"])
    assert rebuilt.exit_code == 0, rebuilt.output

    with JobStore(journal) as store:
        assert store.load("j1").status == "succeeded", (
            "the rebuild replaced the database the journal lived in"
        )
