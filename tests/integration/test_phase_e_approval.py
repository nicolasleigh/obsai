"""Phase E-3 integration tests: Human-in-the-Loop (HITL) approval, diff review, and Vault byte invariants.

Acceptance criteria:
1. When write approval is declined (approved=False), the workflow safely terminates and the Vault
   directory tree remains strictly 100% byte-identical (verified via recursive SHA-256 hashing).
2. Pending approval state and diff preview are 100% recoverable across browser refresh / GET request
   directly from the SQLite checkpoint.
3. When write approval is confirmed (approved=True), the note is atomically committed to the Vault.
4. Resuming an already resumed/completed run is rejected with HTTP 400 (not awaiting approval).
"""

from __future__ import annotations

import hashlib
import os
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


def snapshot(root: Path) -> dict[str, str]:
    """Calculate recursive SHA-256 tree digest of all files in root."""
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


def setup_vault_and_app(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, Settings, TestClient]:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        full = vault / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    db_path = tmp_path / "index.db"
    with Database(db_path) as db:
        IncrementalIndexer(IndexRepository(db)).update(vault)

    settings = Settings(vault={"path": vault}, index={"database": db_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return vault, db_path, settings, TestClient(app, base_url=BASE_URL)


def test_agent_approval_decline_preserves_vault_sha256_invariant(tmp_path: Path) -> None:
    """Declining an agent write approval MUST preserve Vault files 100% byte-identical."""
    initial_files = {
        "Notes/Alpha.md": "# Alpha Note\n\nPreserved content.\n",
        "Notes/Beta.md": "# Beta Note\n\nAnother note.\n",
        "Archive/Doc.md": "# Archive\n\nOld archive.\n",
    }
    vault, db_path, settings, client = setup_vault_and_app(tmp_path, initial_files)

    # Compute baseline SHA-256 tree snapshot
    baseline_snapshot = snapshot(vault)

    # Mock agent planner to propose creating a new file
    decision = ToolDecision(
        "create_note",
        {"path": "Notes/DeclinedNote.md", "content": "# Should Never Touch Disk\n\nForbidden bytes.\n"},
    )

    with patch("obsai.agent.openai_planner.OpenAIDecisionProvider.decide", return_value=decision):
        # 1. Start agent run that triggers write approval interrupt
        resp = client.post(
            "/api/v1/agent/runs",
            json={"query": "创建 Notes/DeclinedNote.md", "thread_id": "run-decline-tree-test"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "interrupted"
        assert data["pending_approval"] is not None
        assert "Forbidden bytes" in data["pending_approval"]["preview"]

        # Snapshot during interrupt must match baseline
        interrupt_snapshot = snapshot(vault)
        assert interrupt_snapshot == baseline_snapshot

        # 2. Decline approval
        resume_resp = client.post(
            "/api/v1/agent/runs/run-decline-tree-test/resume",
            json={"approved": False},
        )
        assert resume_resp.status_code == 200, resume_resp.text
        resume_data = resume_resp.json()
        assert resume_data["status"] == "completed"
        assert "Write cancelled by user." in resume_data["final_answer"]

        # 3. CRITICAL SECURITY RED LINE: Vault MUST remain 100% byte-identical
        post_decline_snapshot = snapshot(vault)
        assert post_decline_snapshot == baseline_snapshot
        assert not (vault / "Notes" / "DeclinedNote.md").exists()


def test_agent_approval_state_recovery_across_refresh(tmp_path: Path) -> None:
    """State recovery: Loading /api/v1/agent/runs/{run_id} restores pending approval & preview from SQLite."""
    initial_files = {
        "Notes/Guide.md": "# Guide\n\nInitial guide.\n",
    }
    vault, db_path, settings, client = setup_vault_and_app(tmp_path, initial_files)

    decision = ToolDecision(
        "update_note",
        {
            "path": "Notes/Guide.md",
            "old": "Initial guide.",
            "new": "Updated guide with new instructions.",
        },
    )

    with patch("obsai.agent.openai_planner.OpenAIDecisionProvider.decide", return_value=decision):
        # 1. Run agent workflow to trigger interrupt
        start_resp = client.post(
            "/api/v1/agent/runs",
            json={"query": "更新 Notes/Guide.md", "thread_id": "run-recovery-test"},
        )
        assert start_resp.status_code == 200
        start_data = start_resp.json()
        assert start_data["status"] == "interrupted"
        original_preview = start_data["pending_approval"]["preview"]
        assert "Updated guide" in original_preview

        # 2. Simulate browser refresh by fetching state by run_id
        get_resp = client.get("/api/v1/agent/runs/run-recovery-test")
        assert get_resp.status_code == 200
        recovered_data = get_resp.json()
        assert recovered_data["run_id"] == "run-recovery-test"
        assert recovered_data["status"] == "interrupted"
        assert recovered_data["pending_approval"] is not None
        assert recovered_data["pending_approval"]["kind"] == "write_approval"
        assert recovered_data["pending_approval"]["preview"] == original_preview
        assert recovered_data["step_count"] == start_data["step_count"]


def test_agent_approval_accept_commits_atomically(tmp_path: Path) -> None:
    """Approving an agent write applies the diff atomically to the Vault."""
    initial_files = {
        "Notes/Existing.md": "# Existing Note\n",
    }
    vault, db_path, settings, client = setup_vault_and_app(tmp_path, initial_files)

    decision = ToolDecision(
        "create_note",
        {"path": "Notes/ApprovedNote.md", "content": "# Approved Note Content\n"},
    )

    with patch("obsai.agent.openai_planner.OpenAIDecisionProvider.decide", return_value=decision):
        # Start and interrupt
        client.post(
            "/api/v1/agent/runs",
            json={"query": "创建 Notes/ApprovedNote.md", "thread_id": "run-approve-test"},
        )

        # Resume with approved=True
        resume_resp = client.post(
            "/api/v1/agent/runs/run-approve-test/resume",
            json={"approved": True},
        )
        assert resume_resp.status_code == 200
        resume_data = resume_resp.json()
        assert resume_data["status"] == "completed"
        assert "Change applied." in resume_data["final_answer"]

        # Note is physically created on disk
        target = vault / "Notes" / "ApprovedNote.md"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "# Approved Note Content\n"

        # Subsequent resume attempt is rejected
        duplicate_resume = client.post(
            "/api/v1/agent/runs/run-approve-test/resume",
            json={"approved": True},
        )
        assert duplicate_resume.status_code == 400
        err_data = duplicate_resume.json()
        assert err_data["error"]["code"] == "config"
        assert "not awaiting approval" in err_data["error"]["message"]
