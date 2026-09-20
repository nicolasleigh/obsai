"""B-1 acceptance: ``GET /api/v1/status`` describes every state instead of failing.

The endpoint's contract is *totality*. An overview screen has to be able to say
"no Vault is configured", "the index has not been built" and "a transaction needs
recovery" — and it can only say those things if none of them is an error response.
Each test below pins one of those states at the HTTP boundary, because a
:class:`StatusView` that is total in isolation proves nothing about the adapter
that renders it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.embedding import build_generation
from obsai.application.index import open_index
from obsai.application.locks import vault_lock
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"
JOURNAL_DIR = ".obsai-transactions"


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A two-note Vault with a built index, and the index path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, "Backend/Redis.md", "---\ntags: [redis]\n---\n# Redis\n\nRedis cache strategy.\n")
    write(vault, "Guides/Deploy.md", "# Deploy\n\nSee [[Backend/Redis]].\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def build_journal(vault: Path, transaction_id: str, status: str) -> None:
    """Write a journal by hand.

    A hand-written journal is the only way to reach these states: the real
    ``TransactionService`` always finishes a transaction it starts, so "a process
    died mid-apply" and "committed but the index was never updated" have no
    in-process producer. The file is exactly what the service leaves behind.
    """
    directory = vault / JOURNAL_DIR / transaction_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "journal.json").write_text(
        json.dumps(
            {
                "id": transaction_id,
                "status": status,
                "applied_count": 1,
                "originals": [{"path": "Backend/Redis.md", "hash": "0" * 64, "snapshot": None, "mode": 420}],
                "changes": [],
                "absent_directories": [],
                "dirty_paths": ["Backend/Redis.md"],
                "error": None,
            }
        ),
        encoding="utf-8",
    )


def client_for(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def status_of(settings: Settings) -> dict:
    with client_for(settings) as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #


def test_health_answers_without_any_configuration() -> None:
    """The probe must not depend on the Vault or the index being usable."""
    with client_for(Settings()) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_lifespan_records_when_the_server_started() -> None:
    payload = status_of(Settings())
    assert payload["started_at"]
    assert payload["uptime_seconds"] >= 0


# --------------------------------------------------------------------------- #
# Vault
# --------------------------------------------------------------------------- #


def test_an_unconfigured_vault_is_reported_not_raised() -> None:
    payload = status_of(Settings())
    assert payload["vault_path"] is None
    assert payload["vault_ready"] is False
    assert payload["recovery_required"] is False
    assert payload["unfinished_transactions"] == []


def test_a_configured_vault_that_vanished_is_reported(tmp_path: Path) -> None:
    settings = Settings(vault={"path": tmp_path / "gone"}, index={"database": tmp_path / "index.db"})
    payload = status_of(settings)
    assert payload["vault_path"] == str(tmp_path / "gone")
    assert payload["vault_ready"] is False


def test_a_ready_vault_is_reported(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    payload = status_of(Settings(vault={"path": vault}, index={"database": database_path}))
    assert payload["vault_path"] == str(vault)
    assert payload["vault_ready"] is True


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


def test_a_missing_index_is_reported(tmp_path: Path) -> None:
    vault, _ = build_vault(tmp_path)
    missing = tmp_path / "not-built.db"
    payload = status_of(Settings(vault={"path": vault}, index={"database": missing}))
    assert payload["index"] == {
        "path": str(missing),
        "exists": False,
        "usable": False,
        "error": None,
        "note_count": 0,
        "chunk_count": 0,
        "vector_count": 0,
        "generation": None,
        "semantic_ready": False,
        "dirty_notes": [],
    }


def test_a_built_index_is_described(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    index = status_of(Settings(vault={"path": vault}, index={"database": database_path}))["index"]
    assert index["exists"] is True
    assert index["usable"] is True
    assert index["error"] is None
    assert index["note_count"] == 2
    assert index["chunk_count"] > 0
    # No embeddings have been generated, so the configured generation must not be
    # advertised — reporting it would read as "semantic search is ready".
    assert index["generation"] is None
    assert index["vector_count"] == 0
    assert index["semantic_ready"] is False


def test_an_unreadable_index_is_reported_with_its_reason(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    database_path.write_bytes(b"this is not a sqlite database")
    payload = status_of(Settings(vault={"path": vault}, index={"database": database_path}))
    index = payload["index"]
    assert index["exists"] is True
    assert index["usable"] is False
    assert index["error"]
    # The Vault half is still reported: one broken component must not blank the page.
    assert payload["vault_ready"] is True


def test_a_generation_without_vectors_is_reported_as_not_semantic_ready(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    settings = Settings(vault={"path": vault}, index={"database": database_path})
    generation = build_generation(settings.embedding)
    with Database(database_path) as database, database.transaction() as connection:
        connection.execute(
            """INSERT INTO embedding_generations
               (id, provider, model, model_version, dimensions, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (generation.id, generation.provider, generation.model,
             generation.model_version, generation.dimensions, "2026-09-14T00:00:00Z"),
        )
    index = status_of(settings)["index"]
    assert index["generation"] == generation.id
    assert index["vector_count"] == 0
    assert index["semantic_ready"] is False


def test_dirty_notes_are_listed(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    with Database(database_path) as database:
        IndexRepository(database).mark_dirty("Backend/Redis.md", "note changed")
    index = status_of(Settings(vault={"path": vault}, index={"database": database_path}))["index"]
    assert index["dirty_notes"] == [
        {"path": "Backend/Redis.md", "reason": "note changed", "marked_at": index["dirty_notes"][0]["marked_at"]}
    ]


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["prepared", "applying", "rolling_back", "recovery_required"])
def test_an_unfinished_transaction_requires_recovery(tmp_path: Path, status: str) -> None:
    vault, database_path = build_vault(tmp_path)
    build_journal(vault, "t1", status)
    payload = status_of(Settings(vault={"path": vault}, index={"database": database_path}))
    assert payload["recovery_required"] is True
    assert [item["transaction_id"] for item in payload["unfinished_transactions"]] == ["t1"]
    assert payload["unfinished_transactions"][0]["status"] == status
    assert payload["unfinished_transactions"][0]["originals"] == [
        {"path": "Backend/Redis.md", "snapshot": None}
    ]
    assert payload["index_dirty_transactions"] == []


@pytest.mark.parametrize("status", ["committed", "index_dirty"])
def test_a_committed_transaction_only_needs_an_index_update(tmp_path: Path, status: str) -> None:
    vault, database_path = build_vault(tmp_path)
    build_journal(vault, "t2", status)
    payload = status_of(Settings(vault={"path": vault}, index={"database": database_path}))
    assert payload["recovery_required"] is False
    assert payload["unfinished_transactions"] == []
    assert [item["transaction_id"] for item in payload["index_dirty_transactions"]] == ["t2"]


def test_a_malformed_journal_does_not_blank_the_page(tmp_path: Path) -> None:
    """``list_journals`` refuses a malformed journal; the rest must survive."""
    vault, database_path = build_vault(tmp_path)
    broken = vault / JOURNAL_DIR / "t3"
    broken.mkdir(parents=True)
    (broken / "journal.json").write_text("{ not json", encoding="utf-8")
    payload = status_of(Settings(vault={"path": vault}, index={"database": database_path}))
    assert payload["vault_ready"] is True
    assert payload["index"]["note_count"] == 2
    assert payload["unfinished_transactions"] == []


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_a_held_write_lock_is_reported(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    settings = Settings(vault={"path": vault}, index={"database": database_path})
    assert status_of(settings)["locked"] is False
    with vault_lock(database_path, operation="index update"):
        assert status_of(settings)["locked"] is True
    assert status_of(settings)["locked"] is False


def test_reading_the_status_does_not_take_the_write_lock(tmp_path: Path) -> None:
    """``/status`` must stay available while a long write is in progress: it is the
    call that explains why the write is taking so long."""
    vault, database_path = build_vault(tmp_path)
    settings = Settings(vault={"path": vault}, index={"database": database_path})
    with vault_lock(database_path, operation="index update", timeout=0.1):
        with client_for(settings) as client:
            assert client.get("/api/v1/status").status_code == 200


# --------------------------------------------------------------------------- #
# The handle the endpoint is given
# --------------------------------------------------------------------------- #


def test_the_request_scoped_handle_closes_its_connection(tmp_path: Path) -> None:
    """A leaked connection would hold the index open past the request."""
    vault, database_path = build_vault(tmp_path)
    handle = open_index(Settings(vault={"path": vault}, index={"database": database_path}))
    connection = handle.require().connection
    handle.close()
    assert handle.database is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
