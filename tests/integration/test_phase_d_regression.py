"""Phase D acceptance regression: Writes & Organization milestone sign-off (D-6).

This suite pins the core acceptance criteria of Phase D (§7 D-6):
1. **拒绝时 Vault 字节不变 (Byte-identity on decline)**:
   - Declining a multi-file plan leaves the Vault 100% byte-for-byte identical (SHA-256 tree compare).
   - The nonce and plan are consumed and cannot be re-approved.
2. **并发修改与漂移返回 409 (Conflict on modified file)**:
   - When a note is modified externally between plan generation and approval, approval is refused
     with HTTP 409 Conflict (code "conflict") and leaves the modified note intact.
3. **写入失败安全回滚 (Atomic rollback on write failure)**:
   - When an unexpected failure occurs midway through applying changes, the Vault is atomically
     rolled back to its pre-transaction state.
4. **提交后索引失败标 dirty (Index update failure marks index_dirty)**:
   - When Vault commit succeeds but index synchronization fails, outcome returns committed=True,
     index_dirty=True, and the transaction is recorded as index_dirty.
5. **未完成事务恢复与防覆盖保护 (Transaction recovery & drift refusal)**:
   - An unfinished/crashed transaction is recoverable via the recovery endpoint;
   - If files on disk were tampered with after the crash, recovery refuses overwrite with HTTP 423.
6. **Inbox 整理至审批执行全链路闭环 (End-to-end organize to approval cycle)**:
   - Scans Inbox, generates read-only proposals, selects proposal numbers, generates ChangePlanView,
     and approves it atomically to disk.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.changes import reset
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.safe_write import ChangeSet
from obsai.safe_write.service import SafeWriteService
from obsai.storage import Database, IndexRepository
from obsai.transactions.journal import TransactionJournal
from obsai.transactions.models import TransactionOperation as Op
from obsai.transactions.service import TransactionService

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


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 1: 拒绝时 Vault 字节不变 (SHA-256 Tree Identity)
# --------------------------------------------------------------------------- #


def test_d6_decline_guarantees_vault_sha256_tree_identity(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Notes/A.md": "# Note A\nOriginal content of A.\n",
            "Notes/B.md": "---\ntags: [old]\n---\n# Note B\n",
            "Notes/C.md": "# Note C\nTo be moved.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before_tree = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        # 创建包含修改、移动与属性更新的复合计划
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "Notes/A.md", "old": "Original content of A.", "new": "Modified A."},
                    {"kind": "move", "path": "Notes/C.md", "destination": "Archive/C.md"},
                    {"kind": "frontmatter", "path": "Notes/B.md", "updates": {"tags": ["old", "new_tag"]}},
                ]
            },
        )
        assert create_res.status_code == 200
        plan = create_res.json()
        plan_id = plan["plan_id"]
        nonce = plan["nonce"]
        revision = plan["revision"]

        # 拒绝计划 (approved=False)
        decline_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={"approved": False, "nonce": nonce, "revision": revision},
        )
        assert decline_res.status_code == 200
        outcome = decline_res.json()
        assert outcome["committed"] is False
        assert outcome["cancelled"] is True

        # 重放拒绝应被拦截 (nonce/plan 已消费)
        replay_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={"approved": True, "nonce": nonce, "revision": revision},
        )
        assert replay_res.status_code in (404, 409)

    # 递归校验：Vault 每一个文件的 SHA-256 均与初始完全一致，没有留下一字节脏数据
    after_tree = snapshot(vault)
    assert after_tree == before_tree
    assert not (vault / ".obsai" / "transactions").exists()


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 2: 并发修改与漂移返回 409 (Conflict on Modified File)
# --------------------------------------------------------------------------- #


def test_d6_content_drift_between_plan_and_approval_returns_409(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {"Notes/Concurrent.md": "# Original\nInitial state.\n"},
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "replace",
                        "path": "Notes/Concurrent.md",
                        "old": "Initial state.",
                        "new": "Expected replacement.",
                    }
                ]
            },
        )
        plan = create_res.json()
        plan_id = plan["plan_id"]

        # 模拟外部编辑器或并发写入者修改了该笔记
        (vault / "Notes/Concurrent.md").write_text("# Externally Modified\nDrifted state.\n", encoding="utf-8")

        # 尝试审批原计划，必须被拦截并返回 409 Conflict
        approve_res = client.post(
            f"/api/v1/changes/plans/{plan_id}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert approve_res.status_code == 409
        body = approve_res.json()
        assert body["error"]["code"] == "conflict"

    # 外部写入的内容完好无损，未被覆盖
    assert (vault / "Notes/Concurrent.md").read_text(encoding="utf-8") == "# Externally Modified\nDrifted state.\n"


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 3: 写入失败安全回滚 (Atomic Rollback on Write Failure)
# --------------------------------------------------------------------------- #


def test_d6_partial_write_failure_rolls_back_vault_atomically(tmp_path: Path, monkeypatch) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Notes/First.md": "# First\nOriginal First.\n",
            "Notes/Second.md": "# Second\nOriginal Second.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before_tree = snapshot(vault)

    # 注入故障：让第二步写入发生异常
    real_apply = SafeWriteService.apply
    apply_call_count = 0

    def fail_second_apply(self, change_set, *, approved=False):
        nonlocal apply_call_count
        apply_call_count += 1
        if apply_call_count == 2:
            raise OSError("Injected disk failure during step 2")
        return real_apply(self, change_set, approved=approved)

    monkeypatch.setattr(SafeWriteService, "apply", fail_second_apply)

    with TestClient(app, base_url=BASE_URL) as client:
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "Notes/First.md", "old": "Original First.", "new": "Modified First."},
                    {"kind": "replace", "path": "Notes/Second.md", "old": "Original Second.", "new": "Modified Second."},
                ]
            },
        )
        plan = create_res.json()

        # 审批执行，底层发生异常并触发自动回滚
        approve_res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        # 服务端返回错误 (500 包含回滚说明)
        assert approve_res.status_code == 500

    # 验证 Vault 原子回滚至事务发生前的状态
    assert (vault / "Notes/First.md").read_text(encoding="utf-8") == "# First\nOriginal First.\n"
    assert (vault / "Notes/Second.md").read_text(encoding="utf-8") == "# Second\nOriginal Second.\n"
    assert snapshot(vault) == before_tree


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 4: 提交后索引失败标 dirty (Index Update Failure)
# --------------------------------------------------------------------------- #


def test_d6_index_failure_after_commit_marks_index_dirty(tmp_path: Path, monkeypatch) -> None:
    vault = make_vault(
        tmp_path,
        {"Notes/Target.md": "# Target\nOld content.\n"},
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    def fail_index_update(self, root):
        raise OSError("Injected SQLite index update failure after commit")

    monkeypatch.setattr(IncrementalIndexer, "update", fail_index_update)

    with TestClient(app, base_url=BASE_URL) as client:
        create_res = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {"kind": "replace", "path": "Notes/Target.md", "old": "Old content.", "new": "New committed content."}
                ]
            },
        )
        plan = create_res.json()

        approve_res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert approve_res.status_code == 200
        outcome = approve_res.json()

        # 验证结果中如实标记 committed=True, index_dirty=True
        assert outcome["committed"] is True
        assert outcome["index_dirty"] is True
        assert "Injected SQLite index update failure" in (outcome["index_error"] or "")

        # 检查 transactions 接口能够看到 index_dirty 记录
        tx_res = client.get("/api/v1/transactions")
        assert tx_res.status_code == 200
        dirty_txs = [tx for tx in tx_res.json() if tx["status"] == "index_dirty"]
        assert len(dirty_txs) == 1

    # Vault 内容已成功写入
    assert (vault / "Notes/Target.md").read_text(encoding="utf-8") == "# Target\nNew committed content.\n"


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 5: 未完成事务恢复与防覆盖保护 (Recovery & Drift Protection)
# --------------------------------------------------------------------------- #


def test_d6_recovery_restores_snapshots_and_refuses_tampered_files(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {"Notes/RecoverMe.md": "# Original Snapshot\nImportant safe data.\n"},
    )
    db_path = tmp_path / "index.db"

    # 模拟未完成崩溃事务 (applying 状态)
    service = TransactionService(vault)
    plan = service.plan([Op.replace("Notes/RecoverMe.md", "Important safe data.", "Corrupted half-written data.")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    # 此时磁盘上是损坏/半写内容
    assert (vault / "Notes/RecoverMe.md").read_text(encoding="utf-8") == "# Original Snapshot\nCorrupted half-written data.\n"

    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        # 1. 查询该事务恢复视图，确认包含 Diff
        rec_view_res = client.get(f"/api/v1/transactions/{tx_id}")
        assert rec_view_res.status_code == 200
        rec_view = rec_view_res.json()
        assert rec_view["recoverable"] is True
        assert len(rec_view["diff"]) > 0

        # 2. 外部篡改：文件在中断后被外部人员修改为未知内容
        (vault / "Notes/RecoverMe.md").write_text("# Tampered externally\n", encoding="utf-8")

        # 尝试恢复：必须返回 423 (RecoveryRequiredError)，拒绝盲目覆盖现场
        tampered_rec_res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": True})
        assert tampered_rec_res.status_code == 423
        assert "Files no longer match a known state" in tampered_rec_res.json()["error"]["message"]

        # 3. 现场保护：文件内容未被破坏
        assert (vault / "Notes/RecoverMe.md").read_text(encoding="utf-8") == "# Tampered externally\n"

        # 4. 纠正回已知中断状态，恢复成功
        (vault / "Notes/RecoverMe.md").write_text("# Original Snapshot\nCorrupted half-written data.\n", encoding="utf-8")
        clean_rec_res = client.post(f"/api/v1/transactions/{tx_id}/recover", json={"approved": True})
        assert clean_rec_res.status_code == 200
        assert clean_rec_res.json()["committed"] is False
        assert clean_rec_res.json()["cancelled"] is False

    # 验证 Vault 彻底还原至初始快照
    assert (vault / "Notes/RecoverMe.md").read_text(encoding="utf-8") == "# Original Snapshot\nImportant safe data.\n"
    assert not TransactionService.journals(vault)


# --------------------------------------------------------------------------- #
# D-6 Core Acceptance 6: Inbox 整理至审批执行全链路闭环 (Organize Cycle)
# --------------------------------------------------------------------------- #


def test_d6_organize_to_approval_end_to_end_cycle(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "---\ntags: [redis, cache]\n---\n# Redis Cache\n\nRedis documentation.\n",
            "Inbox/redis_notes.md": "# Redis Notes\n\nMeeting notes regarding Redis cache design.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before_tree = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        # 1. 扫描提案 (只读)
        proposals_res = client.post("/api/v1/organize/proposals")
        assert proposals_res.status_code == 200
        preview = proposals_res.json()
        assert len(preview["proposals"]) == 1
        assert preview["proposals"][0]["actionable"] is True
        proposal_num = preview["proposals"][0]["number"]

        # 扫描后 Vault 字节完全不变
        assert snapshot(vault) == before_tree

        # 2. 生成变更计划
        plan_res = client.post("/api/v1/organize/plan", json={"numbers": [proposal_num]})
        assert plan_res.status_code == 200
        plan = plan_res.json()
        assert plan["plan_id"]
        assert len(plan["diff"]) > 0

        # 3. 审批并应用计划
        approve_res = client.post(
            f"/api/v1/changes/plans/{plan['plan_id']}/approve",
            json={
                "approved": True,
                "revision": plan["revision"],
                "nonce": plan["nonce"],
            },
        )
        assert approve_res.status_code == 200
        outcome = approve_res.json()
        assert outcome["committed"] is True
        assert outcome["cancelled"] is False

    # 4. 验证文件已成功移出 Inbox，并打上对应标签
    assert not (vault / "Inbox/redis_notes.md").exists()
    dest_file = vault / preview["proposals"][0]["destination"]
    assert dest_file.exists()
    content = dest_file.read_text(encoding="utf-8")
    assert "tags:" in content
    assert "redis" in content
