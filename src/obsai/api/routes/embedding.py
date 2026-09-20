"""Embedding plan preparation and approval routes.

`POST /embedding/plans` computes the plan and tokens/costs preflight.
`POST /embedding/plans/{plan_id}/approve` checks for drift, accepts the work,
submits the job to JobRunner, and returns 202 Accepted with the JobView.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import check_write_lock, get_job_runner, get_settings
from obsai.application.dto import EmbeddingApproveRequest, EmbeddingPlanView, JobView
from obsai.application.embedding_jobs import (
    approve_embedding_plan,
    create_embedding_plan,
)
from obsai.application.jobs import JobRunner
from obsai.config.models import Settings

router = APIRouter(tags=["embedding"])

ACCEPTED = 202


@router.post("/embedding/plans", response_model=EmbeddingPlanView)
def plan_embedding(
    settings: Settings = Depends(get_settings),
) -> EmbeddingPlanView:
    """Preflight and prepare an embedding generation plan."""
    return create_embedding_plan(settings)


@router.post("/embedding/plans/{plan_id}/approve", response_model=JobView, status_code=ACCEPTED)
def approve_plan(
    plan_id: str,
    request: EmbeddingApproveRequest,
    settings: Settings = Depends(get_settings),
    runner: JobRunner = Depends(get_job_runner),
    _lock: None = Depends(check_write_lock),
) -> JobView:
    """Verify approval nonce, guard against plan drift, and start embedding."""
    return approve_embedding_plan(plan_id, request.nonce, settings, runner)
