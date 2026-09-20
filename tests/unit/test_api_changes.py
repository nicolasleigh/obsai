"""Unit tests for change plan preparation, retrieval, and approval routes (Phase D-1).

Tests the contract, security, and lifecycle of write plans:
- Plan generation (POST /changes/plans)
- Plan inspection (GET /changes/plans/{id})
- Plan not found (404) & plan expired (404)
- Approval execution via echoed credentials (POST /changes/plans/{id}/approve)
- Rejection of tampered revision (409)
- Rejection of invalid nonce (409)
- Rejection of replayed nonce (409)
- Rejection when expired (404)
- Clean decline without modifying Vault (200, cancelled=True)
- External Vault lock blocks approve with HTTP 423
- OCC conflict when source file is modified concurrently (409)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.changes import create_change_plan, reset
from obsai.application.locks import vault_lock
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository
from obsai.transactions.models import TransactionOperation

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def clean_plan_store():
    reset()
    yield
    reset()


def make_vault(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    if files:
        for rel_path, content in files.items():
            full = vault / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
    return vault


def build_app(vault: Path, db_path: Path):
    with Database(db_path) as db:
        IncrementalIndexer(IndexRepository(db)).update(vault)

    settings = Settings(vault={"path": vault}, index={"database": db_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_post_change_plan_success(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {"Notes/Alpha.md": "# Alpha\n\nOriginal text.\n"},
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "replace",
                        "path": "Notes/Alpha.md",
                        "old": "Original text.",
                        "new": "Updated alpha text.\nExtra line.",
                    },
                    {
                        "kind": "create",
                        "path": "Notes/Beta.md",
                        "new": "# Beta\n\nBeta content.\n",
                    },
                ]
            },
        )

        assert response.status_code == 200
        plan = response.json()
        assert plan["plan_id"]
        assert plan["revision"] == 1
        assert plan["nonce"]
        assert plan["batch"] is True
        assert set(plan["affected_paths"]) == {"Notes/Alpha.md", "Notes/Beta.md"}
        assert len(plan["changes"]) == 2

        # diff_summary verification
        summary_by_path = {item["path"]: item for item in plan["diff_summary"]}
        assert "Notes/Alpha.md" in summary_by_path
        assert "Notes/Beta.md" in summary_by_path
        assert summary_by_path["Notes/Beta.md"]["added_lines"] >= 2
        assert len(plan["diff"]) > 0


def test_get_change_plan_success(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n\nContent.\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        created = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "append", "path": "A.md", "new": "Appended.\n"}
                ]
            },
        ).json()

        plan_id = created["plan_id"]
        retrieved = client.get(f"/api/v1/changes/plans/{plan_id}")
        assert retrieved.status_code == 200
        data = retrieved.json()
        assert data["plan_id"] == plan_id
        assert data["nonce"] == created["nonce"]
        assert data["revision"] == created["revision"]
        assert data["affected_paths"] == created["affected_paths"]


def test_get_change_plan_unknown_returns_404(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/api/v1/changes/plans/nonexistent-plan-id")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "plan_not_found"


def test_get_change_plan_expired_returns_404(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    settings = Settings(vault={"path": vault}, index={"database": db_path})

    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    plan_view = create_change_plan(
        settings,
        [TransactionOperation.create("B.md", "# B\n")],
        now=expired_time,
    )

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get(f"/api/v1/changes/plans/{plan_view.plan_id}")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "plan_expired"


def test_approve_change_plan_executes_and_modifies_vault(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# Original\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "A.md", "old": "# Original\n", "new": "# Modified\n"},
                    {"kind": "create", "path": "New.md", "new": "# New file\n"},
                ]
            },
        ).json()

        # Execute approval
        res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res.status_code == 200
        outcome = res.json()
        assert outcome["plan_id"] == plan["plan_id"]
        assert outcome["committed"] is True
        assert outcome["cancelled"] is False
        assert outcome["transaction_id"] is not None

        # Verify Vault on disk was mutated
        assert (vault / "A.md").read_text(encoding="utf-8") == "# Modified\n"
        assert (vault / "New.md").read_text(encoding="utf-8") == "# New file\n"


def test_decline_change_plan_leaves_vault_intact(tmp_path: Path) -> None:
    original_text = "# Original\nDo not change.\n"
    vault = make_vault(tmp_path, {"A.md": original_text})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "A.md", "old": "Do not change.", "new": "Changed!"}
                ]
            },
        ).json()

        res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": False,
            },
        )
        assert res.status_code == 200
        outcome = res.json()
        assert outcome["committed"] is False
        assert outcome["cancelled"] is True

        # Vault remains byte-identical
        assert (vault / "A.md").read_text(encoding="utf-8") == original_text

        # Plan is now resolved, cannot be approved afterwards
        re_approve = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert re_approve.status_code == 409
        assert re_approve.json()["error"]["code"] == "conflict"


def test_approve_change_plan_tampered_revision_returns_409(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={"operations": [{"kind": "append", "path": "A.md", "new": "B\n"}]},
        ).json()

        res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": 999,
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"


def test_approve_change_plan_invalid_nonce_returns_409(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={"operations": [{"kind": "append", "path": "A.md", "new": "B\n"}]},
        ).json()

        res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": "forged-nonce-123",
                "approved": True,
            },
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"


def test_approve_change_plan_replayed_nonce_returns_409(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={"operations": [{"kind": "append", "path": "A.md", "new": "B\n"}]},
        ).json()

        # First execution succeeds
        res1 = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res1.status_code == 200

        # Replay attempt fails with 409
        res2 = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res2.status_code == 409
        assert res2.json()["error"]["code"] == "conflict"


def test_approve_change_plan_expired_returns_404(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    settings = Settings(vault={"path": vault}, index={"database": db_path})

    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    plan_view = create_change_plan(
        settings,
        [TransactionOperation.create("B.md", "# B\n")],
        now=expired_time,
    )

    app = build_app(vault, db_path)
    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post(
            f"/api/v1/changes/plans/{plan_view.plan_id}/approve",
            json={
                "revision": plan_view.revision,
                "nonce": plan_view.nonce,
                "approved": True,
            },
        )
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "plan_expired"


def test_approve_change_plan_fails_with_423_when_vault_locked(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# A\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={"operations": [{"kind": "append", "path": "A.md", "new": "B\n"}]},
        ).json()

        # Hold lock externally (simulating CLI transaction or index rebuild)
        with vault_lock(db_path, operation="CLI process"):
            res = client.post(
                f"/api/v1/changes/plans/{plan['plan_id']}/approve",
                json={
                    "revision": plan["revision"],
                    "nonce": plan["nonce"],
                    "approved": True,
                },
            )
            assert res.status_code == 423
            assert res.json()["error"]["code"] == "lock_busy"

        # Lock released, approval succeeds
        res_after = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res_after.status_code == 200
        assert res_after.json()["committed"] is True


def test_approve_change_plan_occ_conflict_rolls_back(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, {"A.md": "# Original text\n"})
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        plan = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "A.md", "old": "Original", "new": "Updated"}
                ]
            },
        ).json()

        # External user edits A.md before approval lands
        (vault / "A.md").write_text("# External modification\n", encoding="utf-8")

        res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "revision": plan["revision"],
                "nonce": plan["nonce"],
                "approved": True,
            },
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"

        # Vault content is preserved as externally modified
        assert (vault / "A.md").read_text(encoding="utf-8") == "# External modification\n"
