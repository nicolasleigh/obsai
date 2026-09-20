"""Transaction journal and recovery routes.

`GET /transactions` lists all journals in the Vault.
`GET /transactions/{id}` returns the recovery preview with rollback diff.
`POST /transactions/{id}/recover` rolls back to snapshots if files match.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from obsai.api.deps import check_write_lock, get_settings
from obsai.application.changes import get_recovery, list_transactions, recover_transaction
from obsai.application.dto import ChangeOutcome, JournalView, RecoverTransactionRequest, RecoveryView
from obsai.application.paths import database_path, require_vault
from obsai.config.models import Settings
from obsai.errors import RecoveryRequiredError, TransactionError

router = APIRouter(tags=["transactions"])


@router.get("/transactions", response_model=list[JournalView])
def get_transactions(
    settings: Settings = Depends(get_settings),
) -> list[JournalView]:
    """List all transaction journals in the Vault."""
    vault = require_vault(settings)
    return list(list_transactions(vault))


@router.get("/transactions/{transaction_id}", response_model=RecoveryView)
def get_transaction(
    transaction_id: str,
    settings: Settings = Depends(get_settings),
) -> RecoveryView:
    """Retrieve recovery status and rollback preview diff for a transaction."""
    vault = require_vault(settings)
    return get_recovery(vault, transaction_id)


@router.post("/transactions/{transaction_id}/recover", response_model=ChangeOutcome)
def post_recover_transaction(
    transaction_id: str,
    request: RecoverTransactionRequest = RecoverTransactionRequest(),
    settings: Settings = Depends(get_settings),
) -> ChangeOutcome:
    """Recover an unfinished transaction by restoring snapshot files."""
    vault = require_vault(settings)
    if request.approved:
        check_write_lock(settings)
    db_path = database_path(settings)
    try:
        return recover_transaction(
            vault, db_path, transaction_id, approved=request.approved
        )
    except RecoveryRequiredError:
        raise
    except TransactionError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
