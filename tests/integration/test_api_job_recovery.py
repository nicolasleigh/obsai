"""C-1: startup recovery, and the promise that a read-only start writes nothing.

Recovery needs a moment at which no request can observe a stale ``running`` job,
and the only such moment is before the server accepts one — hence the lifespan.

The second test is the counterweight. The same startup hook runs on a machine that
has never run a job, where the journal does not exist yet, and "were there jobs?"
must not be answered by creating the file that would make the answer yes. That is
the read-only guarantee B-9 established, restated for a new startup side effect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.application.dto import JobView
from obsai.application.jobs import JobStore, job_journal_path
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def build(tmp_path: Path) -> Path:
    """A one-note Vault with a built index, plus a config that points at both."""
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


def test_startup_closes_out_jobs_the_previous_process_left_running(tmp_path: Path) -> None:
    database_path = build(tmp_path)
    journal = job_journal_path(database_path)
    with JobStore(journal) as store:
        store.save(view("j1", "running"))
        store.save(view("j2", "awaiting_approval"))
        store.save(view("j3", "succeeded"))

    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/health").status_code == 200

    with JobStore(journal) as store:
        assert store.load("j1").status == "interrupted"
        assert store.load("j2").status == "interrupted"
        assert store.load("j3").status == "succeeded"


def test_startup_creates_no_journal_on_a_read_only_path(tmp_path: Path) -> None:
    database_path = build(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}

    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/status").status_code == 200

    assert not job_journal_path(database_path).exists()
    assert {path.name for path in tmp_path.iterdir()} == before


def test_a_broken_journal_does_not_stop_the_server(tmp_path: Path) -> None:
    database_path = build(tmp_path)
    # What a half-written file looks like: not a database at all.
    job_journal_path(database_path).write_bytes(b"not a database")

    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/status").status_code == 200


def view(job_id: str, status: str) -> JobView:
    return JobView(
        job_id=job_id,
        kind="embedding",
        status=status,
        created_at=NOW,
        started_at=NOW,
    )
