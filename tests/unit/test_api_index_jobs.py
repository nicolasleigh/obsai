"""C-3b: the two index endpoints accept work, and refuse it before queueing.

Three things are pinned here.

**202 means accepted.** The response arrives while the work is still running, and it
carries a job the caller can already read. ``test_the_response_says_accepted_rather
_than_finished`` holds the job inside its first step so the assertion cannot be
satisfied by an update that happened to finish first.

**A refusal is a request error, not a failed job.** A Vault that is missing, and a
Vault frozen for recovery, both produce an answer about the *request* — 400 and 423 —
and leave no job behind. Asserting the status alone would not catch the failure mode
that matters: a handler that queues first and validates inside the callable answers
the same 400 for the first request and then leaves a doomed record for the next
restart to pick up. So each refusal test also checks ``/jobs`` is empty and that no
journal file was created.

**No index is required.** Building it for the first time and synchronising one are
the same operation, which is why these routes depend on configuration rather than on
an index handle. A fresh machine — the one the overview screen is telling to run
``obsai index update`` — has to be able to press the button.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.application.dto import JobView
from obsai.application.index_jobs import EMBEDDINGS_STALE
from obsai.application.jobs import job_journal_path
from obsai.transactions import TransactionOperation as Op
from obsai.transactions import TransactionService
from obsai.transactions.journal import TransactionJournal

BASE_URL = "http://127.0.0.1:8000"


def make_vault(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    for name in names:
        (root / name).write_text(f"# {name}\n\n{name} body.\n", encoding="utf-8")
    return root


def write_config(tmp_path: Path, vault: Path, database_path: Path) -> None:
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )


def wait_until(predicate, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def freeze_for_recovery(vault: Path) -> None:
    """Leave an unfinished transaction behind without touching a single file."""
    service = TransactionService(vault)
    plan = service.plan([Op.create("Later.md", "content")])
    TransactionJournal.create(service.root, uuid4().hex, plan).update(status="applying")


def settled(client: TestClient, job_id: str) -> dict:
    wait_until(lambda: client.get(f"/api/v1/jobs/{job_id}").json()["status"] not in ("queued", "running"))
    return client.get(f"/api/v1/jobs/{job_id}").json()


# --------------------------------------------------------------------------- #
# Accepted
# --------------------------------------------------------------------------- #


def test_the_response_says_accepted_rather_than_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)

    import obsai.application.index_jobs as module

    real_scan = module.scan_markdown_files
    gate = threading.Event()

    def blocked_scan(root: Path):
        # Holds the job inside its first step, so "the response arrived before the
        # work finished" is a fact about the endpoint and not about how fast the
        # machine is.
        gate.wait(timeout=10)
        return real_scan(root)

    monkeypatch.setattr(module, "scan_markdown_files", blocked_scan)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/jobs/index-update")
        try:
            assert response.status_code == 202
            payload = response.json()
            assert payload["kind"] == "index-update"
            assert JobView(**payload).terminal is False
        finally:
            gate.set()

        finished = settled(client, payload["job_id"])

    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["detail"]["notes"] == 1
    assert finished["detail"]["created"] == 1


def test_a_first_ever_update_builds_the_index(tmp_path: Path) -> None:
    """The index does not have to exist: the button works on an empty machine."""
    vault = make_vault(tmp_path, "A.md", "B.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/jobs/index-update")
        assert response.status_code == 202
        finished = settled(client, response.json()["job_id"])

    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["detail"]["notes"] == 2
    assert database_path.is_file()


def test_a_rebuild_reports_the_embeddings_are_gone(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        first = client.post("/api/v1/jobs/index-update")
        assert settled(client, first.json()["job_id"])["status"] == "succeeded"

        rebuild = client.post("/api/v1/jobs/index-rebuild")
        assert rebuild.status_code == 202
        assert rebuild.json()["kind"] == "index-rebuild"
        finished = settled(client, rebuild.json()["job_id"])

    assert finished["status"] == "succeeded", finished.get("error")
    assert finished["detail"][EMBEDDINGS_STALE] is True
    assert finished["detail"]["created"] == 1


# --------------------------------------------------------------------------- #
# Refused, before anything is queued
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("endpoint", ["index-update", "index-rebuild"])
def test_a_missing_vault_is_a_400_and_queues_nothing(tmp_path: Path, endpoint: str) -> None:
    absent = tmp_path / "absent"
    database_path = tmp_path / "index.db"
    write_config(tmp_path, absent, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post(f"/api/v1/jobs/{endpoint}")
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "config"
        assert error["type"] == "ConfigError"
        assert str(absent) in error["message"]
        assert client.get("/api/v1/jobs").json() == []

    assert not job_journal_path(database_path).exists()


@pytest.mark.parametrize("endpoint", ["index-update", "index-rebuild"])
def test_a_frozen_vault_is_a_423_and_queues_nothing(tmp_path: Path, endpoint: str) -> None:
    """Writes are frozen until the transaction is recovered; the button says so."""
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    freeze_for_recovery(vault)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post(f"/api/v1/jobs/{endpoint}")
        assert response.status_code == 423
        assert response.json()["error"]["code"] == "recovery_required"
        assert client.get("/api/v1/jobs").json() == []

    assert not job_journal_path(database_path).exists()
