"""Embedding plan preparation, approval protocol, and execution job.

Plans are prepared with cost and token preflight checks, bound to an in-memory
registry with a nonce and expiration, and executed through JobRunner once
approved. A drift check guards the approval: if notes, chunks, or configuration
changed between planning and approval, the change is refused with PlanDriftError
and an updated plan is registered for re-confirmation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Any
from uuid import uuid4

from obsai.application.dto import EmbeddingPlanView, JobView
from obsai.application.embedding import build_embedding_pipeline
from obsai.application.index_jobs import STEP_DONE
from obsai.application.jobs import JobCallable, JobContext, JobRunner
from obsai.application.locks import vault_lock
from obsai.application.paths import database_path, require_existing_vault
from obsai.config.models import Settings
from obsai.embedding.pipeline import EmbeddingPlan
from obsai.errors import ConflictError, PlanDriftError
from obsai.storage import Database
from obsai.transactions import TransactionService

EMBEDDING_KIND = "embedding"
STEP_EMBEDDING = "embedding"

PLAN_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class _Entry:
    view: EmbeddingPlanView
    plan: EmbeddingPlan


class EmbeddingPlanStore:
    """Thread-safe registry of embedding plans awaiting approval."""

    def __init__(self, ttl: timedelta = PLAN_TTL) -> None:
        self.ttl = ttl
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def add(self, entry: _Entry) -> EmbeddingPlanView:
        with self._lock:
            self._sweep()
            self._entries[entry.view.plan_id] = entry
        return entry.view

    def get(self, plan_id: str) -> _Entry | None:
        with self._lock:
            self._sweep()
            return self._entries.get(plan_id)

    def discard(self, plan_id: str) -> None:
        with self._lock:
            self._entries.pop(plan_id, None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def _sweep(self) -> None:
        moment = datetime.now(timezone.utc)
        for key in [k for k, v in self._entries.items() if v.view.expires_at <= moment]:
            del self._entries[key]


_store = EmbeddingPlanStore()


def reset_plan_store() -> None:
    """Drop every pending plan. Intended for tests and process teardown."""
    _store.reset()


def _make_view(plan: EmbeddingPlan, *, now: datetime | None = None) -> EmbeddingPlanView:
    moment = now or datetime.now(timezone.utc)
    return EmbeddingPlanView(
        plan_id=uuid4().hex,
        generation_id=plan.generation.id,
        chunks_requiring_embeddings=plan.chunks_requiring_embeddings,
        cache_hits=plan.cache_hits,
        estimated_tokens=plan.estimated_tokens,
        request_count=plan.request_count,
        estimated_cost_usd=plan.estimated_cost_usd,
        expires_at=moment + _store.ttl,
        nonce=uuid4().hex,
    )


def create_embedding_plan(settings: Settings, *, now: datetime | None = None) -> EmbeddingPlanView:
    """Calculate and register a new embedding plan for user confirmation."""
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    TransactionService(vault).ensure_ready()

    with Database(index_path) as database:
        _, pipeline = build_embedding_pipeline(database, settings)
        plan = pipeline.plan()

    view = _make_view(plan, now=now)
    _store.add(_Entry(view=view, plan=plan))
    return view


def approve_embedding_plan(
    plan_id: str,
    nonce: str,
    settings: Settings,
    runner: JobRunner,
) -> JobView:
    """Verify approval, check for plan drift, and submit the embedding job."""
    entry = _store.get(plan_id)
    if entry is None:
        raise ConflictError("This embedding plan is unknown or already resolved; prepare it again")
    if entry.view.nonce != nonce:
        raise ConflictError("Approval does not match the prepared embedding plan")
    if entry.view.expires_at <= datetime.now(timezone.utc):
        raise ConflictError("The embedding plan expired; prepare it again")

    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    TransactionService(vault).ensure_ready()

    with Database(index_path) as database:
        _, pipeline = build_embedding_pipeline(database, settings)
        fresh_plan = pipeline.plan()

    drift = (
        fresh_plan.generation.id != entry.view.generation_id
        or fresh_plan.chunks_requiring_embeddings != entry.view.chunks_requiring_embeddings
        or fresh_plan.cache_hits != entry.view.cache_hits
        or fresh_plan.estimated_tokens != entry.view.estimated_tokens
        or fresh_plan.request_count != entry.view.request_count
        or fresh_plan.estimated_cost_usd != entry.view.estimated_cost_usd
    )

    if drift:
        _store.discard(plan_id)
        fresh_view = _make_view(fresh_plan)
        _store.add(_Entry(view=fresh_view, plan=fresh_plan))
        raise PlanDriftError(
            "The embedding plan has drifted; review and approve the updated plan",
            details=fresh_view.model_dump(mode="json"),
        )

    _store.discard(plan_id)
    work = embedding_job(settings, fresh_plan)
    return runner.submit(EMBEDDING_KIND, work)


def embedding_job(settings: Settings, plan: EmbeddingPlan) -> JobCallable:
    """Build the embedding background job callable."""
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    TransactionService(vault).ensure_ready()

    def work(context: JobContext) -> dict[str, Any]:
        total_batches = len(plan.batches)
        context.progress(STEP_EMBEDDING, completed=0, total=total_batches or 1)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with vault_lock(index_path, operation="index embeddings"):
            TransactionService(vault).ensure_ready()
            with Database(index_path) as database:
                _, pipeline = build_embedding_pipeline(database, settings)

                def on_batch(done: int, total: int) -> None:
                    context.progress(STEP_EMBEDDING, completed=done, total=total)

                attached = asyncio.run(
                    pipeline.execute(plan, approved=True, on_batch=on_batch)
                )
        context.progress(STEP_DONE)
        return {
            "attached": attached,
            "chunks": len(plan.pending_chunks),
            "cache_hits": plan.cache_hits,
        }

    return work
