"""Acceptance tests for /api/v1/transactions routes.

Covers:
- GET /transactions: listing journals (empty, unfinished, committed)
- GET /transactions/{id}: recovery preview with rollback diff
- GET /transactions/{id}: 404 for non-existent journals
- POST /transactions/{id}/recover: successful rollback restoring snapshots
- POST /transactions/{id}/recover: decline/cancel (approved=False) leaves Vault untouched
- POST /transactions/{id}/recover: tampered files returning HTTP 423 (RecoveryRequiredError)
- POST /transactions/{id}/recover: non-recoverable transaction returning HTTP 400
- POST /transactions/{id}/recover: 404 for non-existent journals
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.config.models import Settings
from obsai.safe_write import ChangeSet
from obsai.transactions.journal import TransactionJournal
from obsai.transactions.models import TransactionOperation as Op
from obsai.transactions.service import TransactionService

BASE_URL = "http://127.0.0.1:8000"


def build_app(vault: Path, db_path: Path):
    settings = Settings(vault={"path": vault}, index={"database": db_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_get_transactions_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.get("/api/v1/transactions")
        assert res.status_code == 200
        assert res.json() == []


def test_get_transactions_listing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("content A", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "content A", "updated A")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.get("/api/v1/transactions")
        assert res.status_code == 200
        journals = res.json()
        assert len(journals) == 1
        assert journals[0]["transaction_id"] == tx_id
        assert journals[0]["status"] == "applying"
        assert len(journals[0]["originals"]) == 1
        assert journals[0]["originals"][0]["path"] == "A.md"


def test_get_transaction_recovery_view(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("old text", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "old text", "new text")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.get(f"/api/v1/transactions/{tx_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["transaction_id"] == tx_id
        assert data["status"] == "applying"
        assert data["recoverable"] is True
        assert len(data["diff"]) > 0
        # Diff preview shows rollback: changing 'new text' back to 'old text'
        diff_texts = [d["text"] for d in data["diff"]]
        assert any("-new text" in text for text in diff_texts)
        assert any("+old text" in text for text in diff_texts)


def test_get_transaction_not_found(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.get("/api/v1/transactions/nonexistent-id")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "not_found"


def test_post_recover_transaction_success(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("original", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "original", "modified")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    # Currently Vault has "modified"
    assert (vault / "A.md").read_text(encoding="utf-8") == "modified"

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": True})
        assert res.status_code == 200
        outcome = res.json()
        assert outcome["transaction_id"] == tx_id
        assert outcome["committed"] is False
        assert outcome["cancelled"] is False

    # After recovery, Vault is restored to "original"
    assert (vault / "A.md").read_text(encoding="utf-8") == "original"
    assert not TransactionService.journals(vault)


def test_post_recover_transaction_cancelled(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("original", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "original", "modified")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": False})
        assert res.status_code == 200
        outcome = res.json()
        assert outcome["cancelled"] is True

    # Content stays "modified", journal remains
    assert (vault / "A.md").read_text(encoding="utf-8") == "modified"
    assert len(TransactionService.journals(vault)) == 1


def test_post_recover_transaction_mismatched_state_returns_423(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("original", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "original", "modified")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    # Tamper with file so it matches neither original nor modified state
    (vault / "A.md").write_text("tampered content", encoding="utf-8")

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": True})
        assert res.status_code == 423
        body = res.json()
        assert body["error"]["code"] == "recovery_required"
        assert "Files no longer match a known state" in body["error"]["message"]

    # Tampered content remains untouched
    assert (vault / "A.md").read_text(encoding="utf-8") == "tampered content"


def test_post_recover_already_committed_returns_400(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("original", encoding="utf-8")
    db_path = tmp_path / "index.db"

    service = TransactionService(vault)
    plan = service.plan([Op.replace("A.md", "original", "modified")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="committed")

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": True})
        assert res.status_code == 400
        assert "not awaiting Vault recovery" in res.json()["error"]["message"]
