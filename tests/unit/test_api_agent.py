"""Unit tests for Agent execution, recovery and HITL approval API routes (Phase E-1).

Tests:
- POST /api/v1/agent/runs creates run and handles direct search without LLM
- POST /api/v1/agent/runs with custom thread_id
- POST /api/v1/agent/runs with duplicate thread_id strictly returns HTTP 409 Conflict
- GET /api/v1/agent/runs/{run_id} returns 404 for unknown run
- GET /api/v1/agent/runs/{run_id} restores full timeline and state from SQLite checkpoint
- Human-in-the-loop: write operation triggers interrupt, status becomes "interrupted"
- GET /api/v1/agent/runs/{run_id} includes pending_approval and diff preview during interrupt
- POST /api/v1/agent/runs/{run_id}/resume with approved=True commits file to Vault
- POST /api/v1/agent/runs/{run_id}/resume with approved=False cancels and preserves Vault
- POST /api/v1/agent/runs/{run_id}/resume when not awaiting approval returns HTTP 400
- POST /api/v1/agent/runs/{run_id}/resume with unknown run_id returns HTTP 404
- Checkpoint database file remains agent-checkpoints.db
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from obsai.agent.workflow import ToolDecision
from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"


def setup_test_vault(tmp_path: Path) -> tuple[Path, Path, Settings]:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "Redis.md").write_text("# Redis Architecture\n\nIn-memory cache.\n", encoding="utf-8")
    (vault / "Python.md").write_text("# Python Guide\n\nPython language notes.\n", encoding="utf-8")

    db_path = tmp_path / "index.db"
    with Database(db_path) as db:
        IncrementalIndexer(IndexRepository(db)).update(vault)

    settings = Settings(vault={"path": vault}, index={"database": db_path})
    return vault, db_path, settings


def build_client(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def test_post_agent_run_direct_search(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    resp = client.post("/api/v1/agent/runs", json={"query": "search Redis"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert data["query"] == "search Redis"
    assert data["step_count"] == 1
    assert data["retrieval_step_count"] == 1
    assert len(data["selected_note_ids"]) >= 1
    assert len(data["timeline"]) >= 1
    assert data["timeline"][0]["tool"] == "search_notes"
    assert "Redis" in data["final_answer"]
    assert data["pending_approval"] is None


def test_post_agent_run_duplicate_thread_id_returns_409(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    # First run succeeds
    resp1 = client.post(
        "/api/v1/agent/runs",
        json={"query": "search Redis", "thread_id": "duplicate-test-id"},
    )
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["run_id"] == "duplicate-test-id"

    # Second run with same thread_id must return 409 Conflict
    resp2 = client.post(
        "/api/v1/agent/runs",
        json={"query": "search Python", "thread_id": "duplicate-test-id"},
    )
    assert resp2.status_code == 409, resp2.text
    data = resp2.json()
    assert data["error"]["code"] == "conflict"
    assert "already exists" in data["error"]["message"]


def test_get_agent_run_not_found(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    resp = client.get("/api/v1/agent/runs/non-existent-run-id")
    assert resp.status_code == 404, resp.text
    data = resp.json()
    assert data["error"]["code"] == "not_found"


def test_get_agent_run_recovers_state_from_checkpoint(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    run_resp = client.post(
        "/api/v1/agent/runs",
        json={"query": "search Python", "thread_id": "recovers-state-id"},
    )
    assert run_resp.status_code == 200
    run_data = run_resp.json()

    # Re-fetch from checkpoint using GET
    get_resp = client.get(f"/api/v1/agent/runs/{run_data['run_id']}")
    assert get_resp.status_code == 200
    recovered = get_resp.json()
    assert recovered["run_id"] == run_data["run_id"]
    assert recovered["status"] == "completed"
    assert recovered["query"] == "search Python"
    assert recovered["step_count"] == 1
    assert recovered["timeline"] == run_data["timeline"]
    assert recovered["final_answer"] == run_data["final_answer"]

    # Verify checkpoint file exists alongside index.db
    checkpoints_db = db_path.with_name("agent-checkpoints.db")
    assert checkpoints_db.is_file()


def test_hitl_write_approval_and_resume_approved(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    decision = ToolDecision("create_note", {"path": "Notes/New.md", "content": "# Hello Agent"})

    with patch("obsai.agent.openai_planner.OpenAIDecisionProvider.decide", return_value=decision):
        # 1. Trigger write operation run
        resp = client.post(
            "/api/v1/agent/runs",
            json={"query": "创建 Notes/New.md", "thread_id": "hitl-write-run"},
        )
        assert resp.status_code == 200, resp.text
        run_data = resp.json()

        assert run_data["status"] == "interrupted"
        assert run_data["pending_approval"] is not None
        assert run_data["pending_approval"]["kind"] == "write_approval"
        assert "Hello Agent" in run_data["pending_approval"]["preview"]
        assert run_data["pending_approval"]["plan_ref"] is not None
        # File must NOT exist yet
        assert not (vault / "Notes" / "New.md").exists()

        # 2. Re-fetch via GET verifies persisted interrupted state
        get_resp = client.get("/api/v1/agent/runs/hitl-write-run")
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["status"] == "interrupted"
        assert get_data["pending_approval"]["preview"] == run_data["pending_approval"]["preview"]

        # 3. Resume with approved=True
        resume_resp = client.post(
            "/api/v1/agent/runs/hitl-write-run/resume",
            json={"approved": True},
        )
        assert resume_resp.status_code == 200, resume_resp.text
        resumed_data = resume_resp.json()
        assert resumed_data["status"] == "completed"
        assert "Change applied." in resumed_data["final_answer"]
        assert (vault / "Notes" / "New.md").exists()
        assert (vault / "Notes" / "New.md").read_text(encoding="utf-8") == "# Hello Agent"


def test_hitl_write_resume_declined(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    decision = ToolDecision("create_note", {"path": "Notes/Declined.md", "content": "# Should Not Exist"})

    with patch("obsai.agent.openai_planner.OpenAIDecisionProvider.decide", return_value=decision):
        resp = client.post(
            "/api/v1/agent/runs",
            json={"query": "创建 Notes/Declined.md", "thread_id": "declined-run"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "interrupted"

        # Resume with approved=False
        resume_resp = client.post(
            "/api/v1/agent/runs/declined-run/resume",
            json={"approved": False},
        )
        assert resume_resp.status_code == 200
        resumed_data = resume_resp.json()
        assert resumed_data["status"] == "completed"
        assert "Write cancelled by user." in resumed_data["final_answer"]
        assert not (vault / "Notes" / "Declined.md").exists()


def test_resume_when_not_awaiting_approval_returns_400(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    # Completed direct search run
    client.post(
        "/api/v1/agent/runs",
        json={"query": "search Redis", "thread_id": "completed-run"},
    )

    # Resuming completed run must fail with 400
    resp = client.post(
        "/api/v1/agent/runs/completed-run/resume",
        json={"approved": True},
    )
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert data["error"]["code"] == "config"
    assert "not awaiting approval" in data["error"]["message"]


def test_resume_non_existent_run_returns_404(tmp_path: Path):
    vault, db_path, settings = setup_test_vault(tmp_path)
    client = build_client(settings)

    resp = client.post(
        "/api/v1/agent/runs/non-existent-run/resume",
        json={"approved": True},
    )
    assert resp.status_code == 404, resp.text
    data = resp.json()
    assert data["error"]["code"] == "not_found"
