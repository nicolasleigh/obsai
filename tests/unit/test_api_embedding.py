"""C-4: Embedding plan preflight, budget checks, approval, and drift detection.

Pinned behaviours:
* POST /embedding/plans returns plan_id, nonce, tokens, requests, and cost.
* Budget exceeded returns 429 (EmbeddingBudgetError).
* Valid approval returns 202 with an accepted JobView (kind="embedding").
* Stale or invalid nonce returns 409 (ConflictError).
* Plan drift between plan and approval returns 409 (PlanDriftError) with fresh details.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.application.dto import EmbeddingPlanView, JobView
from obsai.application.embedding_jobs import reset_plan_store
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionOperation as Op
from obsai.transactions import TransactionService
from obsai.transactions.journal import TransactionJournal

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def clean_plans():
    reset_plan_store()
    yield
    reset_plan_store()


def make_vault(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    for name in names:
        (root / name).write_text(f"# {name}\n\n{name} body text.\n", encoding="utf-8")
    return root


def write_config(
    tmp_path: Path,
    vault: Path,
    database_path: Path,
    *,
    cost_limit: float | None = None,
    token_limit: int | None = None,
) -> None:
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        '[embedding]\nprovider = "openai"\nmodel = "text-embedding-3-small"\n',
    ]
    if cost_limit is not None:
        lines.append(f"estimated_cost_limit_usd = {cost_limit}\n")
    if token_limit is not None:
        lines.append(f"max_embedding_tokens = {token_limit}\n")
    config.write_text("".join(lines), encoding="utf-8")


def populate_index(vault: Path, database_path: Path) -> None:
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)


def test_embedding_plan_generation_returns_valid_view(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md", "B.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/embedding/plans")
        assert response.status_code == 200, response.text
        data = response.json()
        view = EmbeddingPlanView(**data)
        assert view.chunks_requiring_embeddings > 0
        assert view.estimated_tokens > 0
        assert view.request_count > 0
        assert view.nonce != ""
        assert view.plan_id != ""
        assert len(view.generation_id) == 64


def test_embedding_plan_budget_exceeded_returns_429(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    # Set impossible budget: 0 tokens
    write_config(tmp_path, vault, database_path, token_limit=0)
    populate_index(vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.post("/api/v1/embedding/plans")
        assert response.status_code == 429
        data = response.json()
        assert data["error"]["code"] == "embedding_budget"


def test_embedding_plan_approval_starts_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        plan_res = client.post("/api/v1/embedding/plans")
        assert plan_res.status_code == 200
        plan = plan_res.json()

        approve_res = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": plan["nonce"]},
        )
        assert approve_res.status_code == 202, approve_res.text
        job = JobView(**approve_res.json())
        assert job.kind == "embedding"
        assert job.status in ("queued", "running")


def test_embedding_plan_approval_invalid_nonce_returns_409(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        plan = client.post("/api/v1/embedding/plans").json()
        res = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": "wrong_nonce"},
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"


def test_embedding_plan_approval_expired_returns_409(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    from obsai.application import embedding_jobs

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with TestClient(create_app(), base_url=BASE_URL) as client:
        # Create plan in the past
        from obsai.config.loader import load_settings
        settings = load_settings()
        plan_view = embedding_jobs.create_embedding_plan(settings, now=past)

        res = client.post(
            f"/api/v1/embedding/plans/{plan_view.plan_id}/approve",
            json={"nonce": plan_view.nonce},
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"


def test_embedding_plan_drift_detection_returns_409_with_new_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        plan = client.post("/api/v1/embedding/plans").json()

        # Mutate vault and index between plan and approve
        (vault / "B.md").write_text("# B\n\nNew file content.\n", encoding="utf-8")
        populate_index(vault, database_path)

        approve_res = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": plan["nonce"]},
        )
        assert approve_res.status_code == 409, approve_res.text
        data = approve_res.json()
        assert data["error"]["code"] == "plan_drift"
        assert "details" in data["error"]
        new_plan = data["error"]["details"]
        assert new_plan["plan_id"] != plan["plan_id"]
        assert new_plan["chunks_requiring_embeddings"] > plan["chunks_requiring_embeddings"]

        # Now approving with the new plan and new nonce succeeds
        second_approve = client.post(
            f"/api/v1/embedding/plans/{new_plan['plan_id']}/approve",
            json={"nonce": new_plan["nonce"]},
        )
        assert second_approve.status_code == 202
        assert second_approve.json()["kind"] == "embedding"


def test_embedding_plan_frozen_for_recovery_returns_423(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    service = TransactionService(vault)
    plan = service.plan([Op.create("Later.md", "content")])
    TransactionJournal.create(service.root, uuid4().hex, plan).update(status="applying")

    with TestClient(create_app(), base_url=BASE_URL) as client:
        res = client.post("/api/v1/embedding/plans")
        assert res.status_code == 423


def test_cancel_embedding_job_via_api(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time
    from obsai.embedding.openai_provider import OpenAIEmbeddingProvider

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    vault = make_vault(tmp_path, "A.md", "B.md", "C.md")
    database_path = tmp_path / "index.db"
    write_config(tmp_path, vault, database_path)
    populate_index(vault, database_path)

    entered = threading.Event()
    release = threading.Event()

    async def blocking_embed(self, texts: list[str]) -> list[list[float]]:
        entered.set()
        for _ in range(50):
            if release.is_set():
                break
            time.sleep(0.05)
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed", blocking_embed)

    with TestClient(create_app(), base_url=BASE_URL) as client:
        plan = client.post("/api/v1/embedding/plans").json()
        approve_res = client.post(
            f"/api/v1/embedding/plans/{plan['plan_id']}/approve",
            json={"nonce": plan["nonce"]},
        )
        assert approve_res.status_code == 202
        job_id = approve_res.json()["job_id"]

        assert entered.wait(timeout=5)

        cancel_res = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancel_res.status_code == 200

        release.set()

        deadline = time.monotonic() + 5
        job_status = "running"
        while time.monotonic() < deadline:
            res = client.get(f"/api/v1/jobs/{job_id}")
            job_status = res.json()["status"]
            if job_status in ("cancelled", "failed", "succeeded"):
                break
            time.sleep(0.05)

        assert job_status == "cancelled"

