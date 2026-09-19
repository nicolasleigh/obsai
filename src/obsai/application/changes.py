"""Prepared write plans and the approval protocol around them.

The CLI used to preview a change and then apply it through the same service
object, with approval expressed as a ``typer.confirm`` boolean. That coupling
made the approval invisible to anything but a terminal.

Here a plan is prepared once, described by a :class:`ChangePlanView`, and applied
only when the caller echoes back the view's ``plan_id``, ``revision`` and
``nonce`` before ``expires_at``. Three consequences matter:

- an approval cannot be replayed — the entry is consumed on use;
- an approval cannot be redirected — a mismatched plan id is rejected;
- a preview that has gone stale cannot be applied — expiry is enforced, not
  merely displayed.

The registry is in-process and deliberately so: it holds live service handles and
Vault paths, which are not serializable and should not be shared across
processes. A restart simply invalidates outstanding prompts.
"""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from obsai.application.dto import (
    ChangeOperationRequest,
    ChangeOutcome,
    ChangePlanView,
    DiffLine,
    DiffSummaryItem,
    JournalOriginal,
    JournalView,
    PlannedChange,
    RecoveryView,
)
from obsai.application.locks import vault_lock
from obsai.application.paths import database_path, require_existing_vault
from obsai.config.models import Settings
from obsai.errors import ConflictError, NotFoundError, PlanExpiredError, PlanNotFoundError, TransactionError
from obsai.safe_write.models import ChangeSet, FileChange, PreviewLine
from obsai.safe_write.service import SafeWriteService, change_preview_lines
from obsai.transactions.journal import TransactionJournal, UNFINISHED
from obsai.transactions.models import TransactionOperation, TransactionPlan
from obsai.transactions.service import (
    TransactionService,
    plan_preview_lines,
    recovery_preview_lines,
)

#: How long a prepared plan stays applicable. Long enough to read a large diff,
#: short enough that a forgotten prompt cannot rewrite the Vault much later.
PLAN_TTL = timedelta(minutes=30)

#: Domain style names are Rich colours; the wire format uses semantic tags.
TO_DIFF_STYLE = {"green": "added", "red": "removed", "cyan": "hunk", "yellow": "notice"}


def to_diff_lines(lines: Sequence[PreviewLine]) -> tuple[DiffLine, ...]:
    """Translate domain preview lines into the wire contract."""
    return tuple(
        DiffLine(text=line.text, style=TO_DIFF_STYLE.get(line.style), highlight=line.highlight)
        for line in lines
    )


def _diff_stats(old: str | None, new: str | None) -> tuple[int, int]:
    """Compute (added_lines, removed_lines) from before/after content."""
    if old is None and new is None:
        return 0, 0
    if old is None:
        return len(new.splitlines() if new else []), 0
    if new is None:
        return 0, len(old.splitlines() if old else [])
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines()))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return added, removed


@dataclass(frozen=True)
class _Entry:
    view: ChangePlanView
    service: SafeWriteService | TransactionService
    change: ChangeSet | None = None
    plan: TransactionPlan | None = None


class PlanStore:
    """Thread-safe registry of plans awaiting approval."""

    def __init__(self, ttl: timedelta = PLAN_TTL) -> None:
        self.ttl = ttl
        self._entries: dict[str, _Entry] = {}
        self._consumed: dict[str, str] = {}
        self._lock = threading.Lock()

    def add(self, entry: _Entry) -> ChangePlanView:
        with self._lock:
            self._sweep()
            self._entries[entry.view.plan_id] = entry
        return entry.view

    def get(self, plan_id: str) -> _Entry:
        """Lookup an active plan without consuming it."""
        with self._lock:
            if plan_id in self._consumed:
                raise PlanNotFoundError(f"Change plan {plan_id} has already been resolved")
            entry = self._entries.get(plan_id)
            self._sweep()
            if entry is None:
                raise PlanNotFoundError(f"Unknown change plan: {plan_id}")
            if entry.view.expires_at <= datetime.now(timezone.utc):
                raise PlanExpiredError("The change plan expired; prepare it again")
            return entry

    def take(self, plan_id: str, revision: int, nonce: str) -> _Entry:
        """Consume the entry, refusing anything that does not match exactly.

        The requested entry is removed *before* the sweep, otherwise an expired
        plan would be swept away and reported as unknown rather than expired —
        which tells the caller to re-prepare for the wrong reason.
        """
        with self._lock:
            if plan_id in self._consumed:
                raise ConflictError("This change plan is unknown or already resolved; prepare it again")
            entry = self._entries.pop(plan_id, None)
            self._sweep()
            if entry is None:
                raise PlanNotFoundError(f"Unknown change plan: {plan_id}")
            if entry.view.expires_at <= datetime.now(timezone.utc):
                raise PlanExpiredError("The change plan expired; prepare it again")
            if entry.view.revision != revision:
                self._entries[plan_id] = entry
                raise ConflictError("The change plan was revised; review it again before approving")
            if entry.view.nonce != nonce:
                self._entries[plan_id] = entry
                raise ConflictError("Approval does not match the prepared change plan")
            self._consumed[plan_id] = nonce
            return entry

    def discard(self, plan_id: str) -> None:
        with self._lock:
            self._entries.pop(plan_id, None)
            self._consumed[plan_id] = ""

    def _sweep(self) -> None:
        moment = datetime.now(timezone.utc)
        for key in [k for k, v in self._entries.items() if v.view.expires_at <= moment]:
            del self._entries[key]


#: Module-level store, matching the module-level plan/approve functions. Call
#: :func:`reset` between tests so approvals cannot leak across cases.
_store = PlanStore()


def reset() -> None:
    """Drop every pending plan. Intended for tests and process teardown."""
    global _store
    _store = PlanStore()


def _view(
    changes: Sequence[FileChange],
    lines: Sequence[PreviewLine],
    *,
    ambiguous_backlinks: Sequence[str] = (),
    batch: bool,
    now: datetime | None = None,
) -> ChangePlanView:
    moment = now or datetime.now(timezone.utc)
    diff_summary = tuple(
        DiffSummaryItem(
            path=change.path,
            operation=change.operation,
            destination=change.destination,
            added_lines=_diff_stats(change.original_content, change.new_content)[0],
            removed_lines=_diff_stats(change.original_content, change.new_content)[1],
        )
        for change in changes
    )
    return ChangePlanView(
        plan_id=uuid4().hex,
        revision=1,
        expires_at=moment + _store.ttl,
        nonce=uuid4().hex,
        changes=tuple(
            PlannedChange(
                operation=change.operation,
                path=change.path,
                destination=change.destination,
                affected_backlinks=tuple(change.affected_backlinks),
            )
            for change in changes
        ),
        affected_paths=tuple(sorted({change.path for change in changes})),
        diff=to_diff_lines(lines),
        diff_summary=diff_summary,
        ambiguous_backlinks=tuple(ambiguous_backlinks),
        batch=batch,
    )


def plan_single(service: SafeWriteService, change: ChangeSet) -> ChangePlanView:
    """Register a one-note change for approval."""
    view = _view([change.file], change_preview_lines(change), batch=False)
    return _store.add(_Entry(view=view, service=service, change=change))


def plan_transaction(service: TransactionService, plan: TransactionPlan) -> ChangePlanView:
    """Register a multi-file transaction for approval."""
    view = _view(
        plan.changes,
        plan_preview_lines(plan),
        ambiguous_backlinks=plan.ambiguous_backlinks,
        batch=True,
    )
    return _store.add(_Entry(view=view, service=service, plan=plan))


def plan_batch(service: TransactionService, operations: Sequence[TransactionOperation]) -> ChangePlanView:
    """Plan and register a transaction from raw operations."""
    return plan_transaction(service, service.plan(operations))


def approve(
    view: ChangePlanView, *, approved: bool = True, nonce: str | None = None
) -> ChangeOutcome:
    """Apply a prepared plan, or decline it and drop it.

    ``nonce`` is not defaulted from ``view``: the whole point of the value is that
    the approver demonstrates it saw *this* plan, and a default would make the
    check decorative. A decline is safe, so it does not have to prove anything.
    """
    if approved and not nonce:
        raise ConflictError("Approval must echo the prepared change plan's nonce")
    entry = _store.take(
        view.plan_id, view.revision, nonce if nonce is not None else view.nonce
    )
    if not approved:
        return ChangeOutcome(plan_id=entry.view.plan_id, committed=False, cancelled=True)

    if entry.change is not None:
        assert isinstance(entry.service, SafeWriteService)
        committed = entry.service.apply(entry.change, approved=True)
        return ChangeOutcome(plan_id=entry.view.plan_id, committed=bool(committed))

    assert entry.plan is not None and isinstance(entry.service, TransactionService)
    result = entry.service.execute(entry.plan, approved=True)
    return ChangeOutcome(
        plan_id=entry.view.plan_id,
        committed=result.committed,
        cancelled=result.cancelled,
        index_dirty=result.index_dirty,
        index_error=result.index_error,
        transaction_id=result.transaction_id,
    )


def create_change_plan(
    settings: Settings,
    operations: Sequence[TransactionOperation | ChangeOperationRequest],
    *,
    now: datetime | None = None,
) -> ChangePlanView:
    """Generate and register a multi-file transaction plan from operations."""
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    service = TransactionService(vault, database_path=index_path)
    service.ensure_ready()
    tx_ops = [
        op
        if isinstance(op, TransactionOperation)
        else TransactionOperation(
            kind=op.kind,
            path=op.path,
            destination=op.destination,
            old=op.old,
            new=op.new,
            updates=op.updates,
        )
        for op in operations
    ]
    plan = service.plan(tx_ops)
    view = _view(
        plan.changes,
        plan_preview_lines(plan),
        ambiguous_backlinks=plan.ambiguous_backlinks,
        batch=True,
        now=now,
    )
    return _store.add(_Entry(view=view, service=service, plan=plan))


def get_change_plan(plan_id: str) -> ChangePlanView:
    """Retrieve an active change plan by ID."""
    entry = _store.get(plan_id)
    return entry.view


def approve_change_plan(
    plan_id: str,
    revision: int,
    nonce: str,
    settings: Settings,
    *,
    approved: bool = True,
) -> ChangeOutcome:
    """Verify echoed credentials and execute the plan through TransactionService."""
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    TransactionService(vault).ensure_ready()

    entry = _store.take(plan_id, revision, nonce)
    if not approved:
        return ChangeOutcome(plan_id=entry.view.plan_id, committed=False, cancelled=True)

    assert entry.plan is not None and isinstance(entry.service, TransactionService)
    with vault_lock(index_path, operation="transaction"):
        result = entry.service.execute(entry.plan, approved=True)
    return ChangeOutcome(
        plan_id=entry.view.plan_id,
        committed=result.committed,
        cancelled=result.cancelled,
        index_dirty=result.index_dirty,
        index_error=result.index_error,
        transaction_id=result.transaction_id,
    )


def discard(view: ChangePlanView) -> None:
    """Forget a plan without applying it."""
    _store.discard(view.plan_id)


def require_batch(view: ChangePlanView) -> None:
    """Guard for callers that can only act on a whole transaction."""
    if not view.batch:
        raise TransactionError("This operation requires a transaction plan")


# --------------------------------------------------------------------------- #
# Journals and recovery
# --------------------------------------------------------------------------- #


def to_journal_view(journal: Mapping) -> JournalView:
    """Convert one raw journal listing into the wire contract."""
    return JournalView(
        transaction_id=journal["id"],
        status=journal["status"],
        originals=tuple(
            JournalOriginal(path=item["path"], snapshot=item.get("snapshot"))
            for item in journal.get("originals", ())
        ),
    )


def journal_views(journals: Sequence[Mapping]) -> tuple[JournalView, ...]:
    return tuple(to_journal_view(journal) for journal in journals)


def recovery_view(service: TransactionService, transaction_id: str) -> RecoveryView:
    """Describe one unfinished transaction, including the rollback diff.

    ``recoverable`` is the same predicate ``transaction recover`` uses to refuse a
    committed journal, so a caller can render the refusal instead of provoking it.
    """
    journal = TransactionJournal.load(service.root, transaction_id)
    recoverable = journal.data["status"] in UNFINISHED
    return RecoveryView(
        transaction_id=transaction_id,
        status=journal.data["status"],
        journal_directory=str(journal.directory),
        originals=tuple(
            JournalOriginal(path=item["path"], snapshot=item.get("snapshot"))
            for item in journal.data["originals"]
        ),
        diff=to_diff_lines(recovery_preview_lines(service, transaction_id)) if recoverable else (),
        recoverable=recoverable,
    )


def list_transactions(vault: Path) -> tuple[JournalView, ...]:
    """List all transaction journals in the Vault."""
    return journal_views(TransactionService.journals(vault))


def get_recovery(vault: Path, transaction_id: str) -> RecoveryView:
    """Retrieve recovery view for a specific transaction in the Vault."""
    service = TransactionService(vault)
    try:
        journal = TransactionJournal(vault, transaction_id)
    except TransactionError:
        raise NotFoundError(f"Transaction journal not found: {transaction_id}")
    if not journal.path.is_file():
        raise NotFoundError(f"Transaction journal not found: {transaction_id}")
    return recovery_view(service, transaction_id)


def recover_transaction(
    vault: Path,
    database_path: Path,
    transaction_id: str,
    *,
    approved: bool = True,
) -> ChangeOutcome:
    """Recover an unfinished transaction by restoring snapshot files."""
    service = TransactionService(vault)
    try:
        journal = TransactionJournal(vault, transaction_id)
    except TransactionError:
        raise NotFoundError(f"Transaction journal not found: {transaction_id}")
    if not journal.path.is_file():
        raise NotFoundError(f"Transaction journal not found: {transaction_id}")

    if approved:
        with vault_lock(database_path, operation="transaction recover"):
            result = service.recover(transaction_id, approved=True)
    else:
        result = service.recover(transaction_id, approved=False)

    return ChangeOutcome(
        plan_id=result.transaction_id,
        transaction_id=result.transaction_id,
        committed=result.committed,
        cancelled=result.cancelled,
        index_dirty=result.index_dirty,
        index_error=result.index_error,
    )

