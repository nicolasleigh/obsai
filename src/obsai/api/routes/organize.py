"""Inbox organization routes.

`POST /organize/proposals` generates read-only classification proposals for notes in the Inbox.
`POST /organize/plan` converts a selection of proposal numbers into an approvable ChangePlanView.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from obsai.api.deps import get_settings, index_handle
from obsai.application.dto import ChangePlanView, OrganizePlanRequest, OrganizePreview
from obsai.application.index import IndexHandle
from obsai.application.organizer import plan_selection, propose
from obsai.application.paths import database_path, require_vault
from obsai.config.models import Settings
from obsai.errors import TransactionError

router = APIRouter(tags=["organize"])


@router.post("/organize/proposals", response_model=OrganizePreview)
def post_organize_proposals(
    settings: Settings = Depends(get_settings),
    index: IndexHandle = Depends(index_handle),
) -> OrganizePreview:
    """Scan the Inbox directory and return proposals (read-only, never modifies Vault)."""
    vault = require_vault(settings)
    db = index.require()
    db_path = database_path(settings)
    scan = propose(vault, db_path, db, settings)
    return scan.preview


@router.post("/organize/plan", response_model=ChangePlanView)
def post_organize_plan(
    request: OrganizePlanRequest,
    settings: Settings = Depends(get_settings),
    index: IndexHandle = Depends(index_handle),
) -> ChangePlanView:
    """Turn selected proposal numbers into a transactional ChangePlanView with nonce."""
    vault = require_vault(settings)
    db = index.require()
    db_path = database_path(settings)
    scan = propose(vault, db_path, db, settings)
    try:
        return plan_selection(scan, list(request.numbers))
    except TransactionError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
