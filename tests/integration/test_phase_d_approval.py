"""Phase D-3 integration tests: Change approval flow, security invariant, and Vault byte-identity.

Acceptance criteria:
1. Declining a plan leaves Vault bytes strictly 100% identical (SHA-256 tree comparison).
2. Approving a plan executes the transaction atomically and applies edits to disk.
3. Tampered revision or invalid nonce is rejected without affecting the Vault.
4. Plan is invalidated and nonces are strictly single-use.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.changes import _store, reset
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def clean_plan_store():
    reset()
    yield
    reset()


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under ``root``, by relative path, with each file's SHA-256 digest."""
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries[relative] = f"symlink -> {os.readlink(path)}"
        elif path.is_dir():
            entries[relative] = "dir"
        else:
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def make_vault(tmp_path: Path, files: dict[str, str]) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
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


def test_decline_leaves_vault_sha256_identical(tmp_path: Path) -> None:
    """Decline must leave the Vault 100% byte-identical, down to SHA-256 digests."""
    initial_files = {
        "Notes/Alpha.md": "# Alpha\n\nOriginal alpha text.\n",
        "Notes/Beta.md": "# Beta\n\nOriginal beta text.\n",
        "Archive/Old.md": "# Old\n\nArchive note.\n",
    }
    vault = make_vault(tmp_path, initial_files)
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before_snapshot = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        # 1. 创建包含新增、修改与删除的综合变更计划
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "create",
                        "path": "Notes/Gamma.md",
                        "new": "# Gamma\n\nProposed new file.\n",
                    },
                    {
                        "kind": "replace",
                        "path": "Notes/Alpha.md",
                        "old": "Original alpha text.",
                        "new": "Modified alpha text.",
                    },
                    {
                        "kind": "trash",
                        "path": "Archive/Old.md",
                    },
                ]
            },
        )
        assert create_res.status_code == 200
        plan = create_res.json()
        plan_id = plan["plan_id"]
        assert len(plan["affected_paths"]) == 3

        # 2. 拒绝该计划 (approved=False)
        decline_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": False,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert decline_res.status_code == 200
        outcome = decline_res.json()
        assert outcome["committed"] is False
        assert outcome["transaction_id"] is None
        assert outcome["plan_id"] == plan_id

        # 3. 校验计划已被废弃，不可再次查询
        get_res = client.get(f"/api/v1/changes/plans/{plan_id}")
        assert get_res.status_code == 404

    # 4. 关键验收：Vault 文件系统 100% 完全相同（无任何新增、修改或临时文件残留）
    after_snapshot = snapshot(vault)
    assert after_snapshot == before_snapshot


def test_approve_creates_single_transaction_and_applies_changes(tmp_path: Path) -> None:
    """Approval must execute the transaction atomically and apply edits to disk."""
    vault = make_vault(
        tmp_path,
        {
            "Docs/Guide.md": "# Guide\n\nVersion 1\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        # 1. 创建写入计划
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "create",
                        "path": "Docs/Changelog.md",
                        "new": "# Changelog\n\nInitial release.\n",
                    },
                    {
                        "kind": "replace",
                        "path": "Docs/Guide.md",
                        "old": "Version 1",
                        "new": "Version 2 updated",
                    },
                ]
            },
        )
        assert create_res.status_code == 200
        plan = create_res.json()
        plan_id = plan["plan_id"]

        # 2. 批准执行
        approve_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert approve_res.status_code == 200
        outcome = approve_res.json()
        assert outcome["committed"] is True
        tx_id = outcome["transaction_id"]
        assert tx_id is not None
        assert len(tx_id) == 32

        # 3. 验证 Vault 文件实际修改生效
        assert (vault / "Docs/Changelog.md").read_text(encoding="utf-8") == "# Changelog\n\nInitial release.\n"
        assert (vault / "Docs/Guide.md").read_text(encoding="utf-8") == "# Guide\n\nVersion 2 updated\n"

        # 4. Nonce 消费防重放：第二次批准同一计划应返回 409 Conflict
        replay_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert replay_res.status_code == 409
        assert replay_res.json()["error"]["code"] == "conflict"


def test_tampered_credentials_rejected_without_vault_change(tmp_path: Path) -> None:
    """Tampered revision or forged nonce must be rejected with 409 and leave Vault unchanged."""
    vault = make_vault(
        tmp_path,
        {"Notes/Secure.md": "# Secure\n\nOriginal.\n"},
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before_snapshot = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "replace",
                        "path": "Notes/Secure.md",
                        "old": "Original.",
                        "new": "Tampered.",
                    }
                ]
            },
        )
        plan = create_res.json()
        plan_id = plan["plan_id"]

        # 1. 篡改 Revision (409)
        bad_rev_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": True,
                "revision": plan["revision"] + 999,
                "nonce": plan["nonce"],
            },
        )
        assert bad_rev_res.status_code == 409

        # 2. 伪造 Nonce (409)
        bad_nonce_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": "forged_nonce_12345",
            },
        )
        assert bad_nonce_res.status_code == 409

    # 3. Vault 完全没有被改变
    assert snapshot(vault) == before_snapshot


def test_expired_plan_rejected_without_vault_change(tmp_path: Path) -> None:
    """Expired plan must be rejected with 404 and leave Vault unchanged."""
    vault = make_vault(
        tmp_path,
        {"Notes/Expiring.md": "# Expiring\n\nStay unchanged.\n"},
    )
    db_path = tmp_path / "index.db"
    settings = Settings(vault={"path": vault}, index={"database": db_path})

    # 生成 1 小时前过期的变更计划
    from obsai.application.changes import create_change_plan
    from obsai.transactions.models import TransactionOperation

    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    plan_view = create_change_plan(
        settings,
        [
            TransactionOperation.replace(
                "Notes/Expiring.md",
                "Stay unchanged.",
                "Should never happen.",
            )
        ],
        now=expired_time,
    )

    app = build_app(vault, db_path)
    before_snapshot = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        approve_res = client.post(
            f"/api/v1/changes/plans/{plan_view.plan_id}/approve",
            json={
                "approved": True,
                "revision": plan_view.revision,
                "nonce": plan_view.nonce,
            },
        )
        assert approve_res.status_code == 404
        assert approve_res.json()["error"]["code"] == "plan_expired"

    # Vault 完全未修改
    assert snapshot(vault) == before_snapshot
