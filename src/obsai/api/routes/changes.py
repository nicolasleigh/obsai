"""Change plan preparation, retrieval, and approval routes.

`POST /changes/plans` computes a transactional change plan with diffs and nonce.
`GET /changes/plans/{plan_id}` retrieves an active change plan.
`POST /changes/plans/{plan_id}/approve` applies or declines an approved change plan
using echoed revision and nonce credentials, delegating exclusively to TransactionService.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import check_write_lock, get_settings
from obsai.application.changes import (
    approve_change_plan,
    create_change_plan,
    get_change_plan,
)
from obsai.application.dto import (
    ApproveChangePlanRequest,
    ChangeOutcome,
    ChangePlanView,
    CreateChangePlanRequest,
)
from obsai.config.models import Settings

router = APIRouter(tags=["changes"])


@router.post("/changes/plans", response_model=ChangePlanView)
def post_change_plan(
    request: CreateChangePlanRequest,
    settings: Settings = Depends(get_settings),
) -> ChangePlanView:
    """Preflight and prepare a multi-file transaction write plan."""
    return create_change_plan(settings, request.operations)


@router.get("/changes/plans/{plan_id}", response_model=ChangePlanView)
def get_change_plan_route(plan_id: str) -> ChangePlanView:
    """Retrieve an active change plan by ID."""
    return get_change_plan(plan_id)


@router.post("/changes/plans/{plan_id}/approve", response_model=ChangeOutcome)
def post_approve_change_plan(
    plan_id: str,
    request: ApproveChangePlanRequest,
    settings: Settings = Depends(get_settings),
) -> ChangeOutcome:
    """Execute or decline an approved change plan using echoed revision and nonce.

    If approved=True, early checks whether another process holds the Vault write lock
    to fail fast with HTTP 423.
    """
    if request.approved:
        check_write_lock(settings)
    return approve_change_plan(
        plan_id,
        request.revision,
        request.nonce,
        settings,
        approved=request.approved,
    )
