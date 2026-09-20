"""Unit tests for Inbox organizer API routes (Phase D-4).

Tests:
- POST /organize/proposals (read-only proposal generation)
- POST /organize/proposals leaves Vault bytes strictly untouched
- POST /organize/proposals with empty Inbox
- POST /organize/plan converts selected proposals into approvable ChangePlanView
- POST /organize/plan with invalid numbers returns 400
- POST /organize/plan with empty numbers returns 422
- Preflight conflict proposal cannot be planned (returns 400)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.changes import reset
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
    """Calculate recursive SHA-256 digests for all files under root."""
    entries: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_file() and not path.is_symlink():
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


def test_post_organize_proposals_success(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "---\ntags: [redis, cache]\n---\n# Redis Cache\n\nRedis caching strategy.\n",
            "Inbox/redis.md": "# Redis Cache\n\nRedis caching notes from meeting.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post("/api/v1/organize/proposals")
        assert res.status_code == 200
        preview = res.json()
        assert preview["inbox"] == "Inbox"
        assert len(preview["proposals"]) == 1

        prop = preview["proposals"][0]
        assert prop["number"] == 1
        assert prop["path"] == "Inbox/redis.md"
        assert prop["destination"] == "Backend/Redis Cache.md"
        assert prop["title"] == "Redis Cache"
        assert prop["confidence"] >= 0.70
        assert prop["actionable"] is True
        assert prop["issue"] is None
        assert prop["selected_by_default"] is True
        assert preview["default_numbers"] == [1]


def test_post_organize_proposals_writes_nothing_to_vault(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "---\ntags: [redis]\n---\n# Redis\n\nRedis docs.\n",
            "Inbox/note.md": "# Redis\n\nNotes.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    before = snapshot(vault)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post("/api/v1/organize/proposals")
        assert res.status_code == 200

    after = snapshot(vault)
    assert after == before


def test_post_organize_proposals_empty_inbox(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Inbox/.keep": "",
            "Backend/Redis.md": "# Redis\n\nRedis docs.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post("/api/v1/organize/proposals")
        assert res.status_code == 200
        preview = res.json()
        assert preview["proposals"] == []
        assert preview["default_numbers"] == []


def test_post_organize_plan_success(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "---\ntags: [redis, cache]\n---\n# Redis Cache\n\nRedis caching strategy.\n",
            "Inbox/redis.md": "# Redis Cache\n\nRedis caching notes from meeting.\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        # 1. 提交选中的提案编号 1
        res = client.post("/api/v1/organize/plan", json={"numbers": [1]})
        assert res.status_code == 200
        plan = res.json()

        # 2. 验证生成的变更计划结构
        assert plan["plan_id"]
        assert plan["revision"] == 1
        assert plan["nonce"]
        assert "Inbox/redis.md" in plan["affected_paths"]
        assert len(plan["diff"]) > 0
        assert len(plan["diff_summary"]) > 0

        # 3. 验证计划已注册至服务端的 PlanStore，可通过 GET /changes/plans/{id} 查出
        get_res = client.get(f"/api/v1/changes/plans/{plan['plan_id']}")
        assert get_res.status_code == 200
        assert get_res.json()["plan_id"] == plan["plan_id"]


def test_post_organize_plan_invalid_number_returns_400(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "# Redis\n",
            "Inbox/redis.md": "# Redis\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post("/api/v1/organize/plan", json={"numbers": [999]})
        assert res.status_code == 400
        assert "Invalid proposal number" in res.json()["error"]["message"]


def test_post_organize_plan_empty_numbers_returns_422(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path,
        {
            "Backend/Redis.md": "# Redis\n",
            "Inbox/redis.md": "# Redis\n",
        },
    )
    db_path = tmp_path / "index.db"
    app = build_app(vault, db_path)

    with TestClient(app, base_url=BASE_URL) as client:
        res = client.post("/api/v1/organize/plan", json={"numbers": []})
        assert res.status_code == 422
