"""Preflighted, journaled multi-file Vault changes with rollback and recovery."""

import difflib
import os
import shutil
import stat
from pathlib import Path
from pathlib import PurePosixPath
from uuid import uuid4
from typing import Callable, Sequence

from rich.console import Console

from obsai.errors import (
    CollisionError, ConflictError, RecoveryRequiredError, SafeWriteError,
    TransactionError,
)
from obsai.safe_write.models import ChangeSet, FileChange
from obsai.safe_write.service import SafeWriteService, _encode, _hash, _sync_directory, patch_frontmatter
from obsai.transactions.backlinks import rewrite_explicit_links
from obsai.transactions.journal import TransactionJournal, UNFINISHED, journal_base, list_journals
from obsai.transactions.models import TransactionOperation, TransactionPlan, TransactionResult
from obsai.vault.scanner import scan_markdown_files


class TransactionService:
    def __init__(
        self, vault_root: Path, *, database_path: Path | None = None,
        indexer: Callable[[Path], object] | None = None,
    ):
        self.safe = SafeWriteService(vault_root)
        self.root = self.safe.root
        self.database_path = database_path
        self.indexer = indexer

    @staticmethod
    def journals(vault_root: Path) -> list[dict]:
        root = vault_root.expanduser().resolve(strict=True)
        return list_journals(root)

    def _ensure_available(self) -> None:
        pending = [item for item in list_journals(self.root) if item["status"] in UNFINISHED]
        if pending:
            ids = ", ".join(item["id"] for item in pending)
            raise RecoveryRequiredError(
                f"Recovery required for transaction(s) {ids}; run 'obsai transaction recover ID'"
            )

    def ensure_ready(self) -> None:
        """Block new writes or reindexing while a Vault transaction is unfinished."""
        self._ensure_available()

    def _path(self, relative: str, *, internal: bool = False) -> Path:
        return self.safe._path(relative, internal=internal)

    def plan(self, operations: Sequence[TransactionOperation]) -> TransactionPlan:
        self._ensure_available()
        if not operations:
            raise TransactionError("Transaction needs at least one operation")
        virtual: dict[str, str | None] = {}
        originals: dict[str, bytes | None] = {}
        original_modes: dict[str, int | None] = {}
        changes: list[FileChange] = []

        def state(path: str, *, internal: bool = False) -> str | None:
            file = self._path(path, internal=internal)
            if path not in virtual:
                if file.exists() or file.is_symlink():
                    content, _ = self.safe._read(file)
                    originals[path] = _encode(content)
                    original_modes[path] = stat.S_IMODE(file.stat().st_mode)
                    virtual[path] = content
                else:
                    self.safe._vacant(file)
                    originals[path] = None
                    original_modes[path] = None
                    virtual[path] = None
            return virtual[path]

        for operation in operations:
            path = operation.path
            current = state(path)
            if operation.kind == "create":
                if current is not None or operation.new is None:
                    raise CollisionError(f"Cannot create occupied or empty proposal: {path}")
                _encode(operation.new)
                changes.append(FileChange("create", path, None, None, None, operation.new))
                virtual[path] = operation.new
            elif operation.kind in ("replace", "append", "rewrite_backlinks", "frontmatter"):
                if current is None:
                    raise ConflictError(f"Note disappeared: {path}")
                if operation.kind == "frontmatter":
                    updated = patch_frontmatter(current, operation.updates)
                    kind = "frontmatter"
                elif operation.kind == "append":
                    if not operation.new:
                        raise SafeWriteError(f"Append requires nonempty content: {path}")
                    updated = current + operation.new
                    kind = "update"
                elif operation.kind == "replace":
                    if not operation.old or current.count(operation.old) != 1 or operation.new is None:
                        raise SafeWriteError(f"Exact replacement must match once: {path}")
                    updated = current.replace(operation.old, operation.new, 1)
                    kind = "update"
                else:
                    if operation.old != current or operation.new is None:
                        raise ConflictError(f"Backlink source changed during planning: {path}")
                    updated = operation.new
                    kind = "update"
                if updated == current:
                    raise SafeWriteError(f"No change to note: {path}")
                _encode(updated)
                changes.append(FileChange(kind, path, None, _hash(_encode(current)), current, updated))
                virtual[path] = updated
            elif operation.kind in ("move", "trash"):
                if current is None:
                    raise ConflictError(f"Note disappeared: {path}")
                destination = operation.destination
                if operation.kind == "trash":
                    destination = f".obsai-trash/{uuid4().hex}/{path}"
                if not destination or destination == path:
                    raise TransactionError("Move requires a distinct destination")
                if state(destination, internal=operation.kind == "trash") is not None:
                    raise CollisionError(f"Destination already exists: {destination}")
                impacts = self.safe._backlink_impact(path) if operation.kind == "move" else ()
                changes.append(FileChange(
                    operation.kind, path, destination, _hash(_encode(current)),
                    current, current if operation.kind == "move" else None, impacts,
                ))
                virtual[path] = None
                virtual[destination] = current
            else:
                raise TransactionError(f"Unknown transaction operation: {operation.kind}")

        finals = {path: _encode(value) if value is not None else None for path, value in virtual.items()}
        absent_dirs: set[str] = set()
        for path in originals:
            parent = self._path(path, internal=path.startswith(".obsai-trash/")).parent
            while parent != self.root and not parent.exists():
                absent_dirs.add(parent.relative_to(self.root).as_posix())
                parent = parent.parent
        return TransactionPlan(
            str(self.root), tuple(operations), tuple(changes), originals, finals, original_modes,
            tuple(sorted(absent_dirs, key=lambda item: (item.count("/"), item))),
        )

    def plan_move_with_backlinks(self, path: str, destination: str) -> TransactionPlan:
        """Move and rewrite only parser-confirmed, explicit vault-root path links."""
        return self.plan_moves_with_backlinks([(path, destination)])

    def plan_moves_with_backlinks(
        self, moves: Sequence[tuple[str, str]],
        extra_operations: Sequence[TransactionOperation] = (),
    ) -> TransactionPlan:
        """Plan a selected batch as one transaction, including conservative backlink rewrites."""
        self._ensure_available()
        if not moves:
            raise TransactionError("At least one move is required")
        if len({source for source, _ in moves}) != len(moves):
            raise TransactionError("A source may be moved only once per transaction")
        if len({destination for _, destination in moves}) != len(moves):
            raise CollisionError("Two moves target the same destination")
        operations = []
        for path, destination in moves:
            source = self._path(path)
            self._path(destination)
            if not source.is_file():
                raise ConflictError(f"Note disappeared: {path}")
            operations.append(TransactionOperation.move(path, destination))
        ambiguous: set[str] = set()
        destination_by_source = dict(moves)
        for candidate in scan_markdown_files(self.root):
            source_path = candidate.relative_to(self.root).as_posix()
            content, _ = self.safe._read(candidate)
            if "[[" not in content:
                continue
            updated = content
            changed = False
            for path, destination in moves:
                if PurePosixPath(path).stem not in updated:
                    continue
                updated, count, ambiguous_count = rewrite_explicit_links(
                    updated, source_path, path, destination
                )
                changed |= bool(count)
                if ambiguous_count:
                    ambiguous.add(source_path)
            if changed:
                output_path = destination_by_source.get(source_path, source_path)
                operations.append(TransactionOperation(
                    "rewrite_backlinks", output_path, old=content, new=updated,
                ))
        operations.extend(extra_operations)
        plan = self.plan(operations)
        return TransactionPlan(
            plan.vault_root, plan.operations, plan.changes, plan.originals,
            plan.finals, plan.original_modes, plan.absent_directories, tuple(sorted(ambiguous)),
        )

    def preview(self, plan: TransactionPlan, console: Console) -> None:
        for change in plan.changes:
            self.safe.preview(ChangeSet(change), console)
        if plan.ambiguous_backlinks:
            console.print("Ambiguous WikiLinks left unchanged in:", style="yellow")
            for path in plan.ambiguous_backlinks:
                console.print(f"  {path}", markup=False)

    def preflight(self, plan: TransactionPlan) -> None:
        self._ensure_available()
        if plan.vault_root != str(self.root):
            raise TransactionError("Transaction plan belongs to another Vault")
        for path, original in plan.originals.items():
            target = self._path(path, internal=path.startswith(".obsai-trash/"))
            if original is None:
                self.safe._vacant(target)
            else:
                self.safe._check_current(target, _hash(original))
                if not os.access(target, os.R_OK):
                    raise TransactionError(f"Note is not readable: {path}")
            ancestor = target.parent
            while not ancestor.exists() and ancestor != self.root:
                ancestor = ancestor.parent
            if not os.access(ancestor, os.W_OK | os.X_OK):
                raise TransactionError(f"Directory is not writable: {ancestor}")
        for change in plan.changes:
            if change.operation in ("move", "trash"):
                assert change.destination is not None
                source = self._path(change.path, internal=change.path.startswith(".obsai-trash/"))
                target_parent = self._path(
                    change.destination, internal=change.operation == "trash"
                ).parent
                source_parent = source if source.exists() else source.parent
                while not source_parent.exists():
                    source_parent = source_parent.parent
                while not target_parent.exists():
                    target_parent = target_parent.parent
                if source_parent.stat().st_dev != target_parent.stat().st_dev:
                    raise TransactionError("Move crosses filesystem devices")
        required = sum(len(content) for content in plan.originals.values() if content is not None)
        required += sum(len(content) for content in plan.finals.values() if content is not None)
        if shutil.disk_usage(self.root).free < required + 4096:
            raise TransactionError("Insufficient free space for transaction snapshots and writes")

    def _current(self, path: str) -> bytes | None:
        target = self._path(path, internal=path.startswith(".obsai-trash/"))
        if not target.exists() and not target.is_symlink():
            return None
        if not stat.S_ISREG(target.lstat().st_mode):
            raise RecoveryRequiredError(f"Transaction path is no longer a regular file: {path}")
        return target.read_bytes()

    @staticmethod
    def _state_after(originals: dict[str, bytes | None], changes: Sequence[FileChange],
                     count: int) -> dict[str, bytes | None]:
        state = dict(originals)
        for change in changes[:count]:
            if change.operation in ("move", "trash"):
                assert change.destination is not None
                state[change.destination] = state[change.path]
                state[change.path] = None
            else:
                state[change.path] = _encode(change.new_content or "")
        return state

    @staticmethod
    def _changed_paths(changes: Sequence[FileChange]) -> set[str]:
        paths = set()
        for change in changes:
            paths.add(change.path)
            if change.destination is not None:
                paths.add(change.destination)
        return paths

    def _verify(self, expected: dict[str, bytes | None], paths: set[str] | None = None) -> None:
        for path in sorted(paths if paths is not None else expected):
            if self._current(path) != expected[path]:
                raise RecoveryRequiredError(f"Transaction file differs from expected state: {path}")

    def _rollback(self, originals: dict[str, bytes | None],
                  original_modes: dict[str, int | None],
                  changes: Sequence[FileChange], applied_count: int,
                  absent_directories: Sequence[str]) -> None:
        paths = self._changed_paths(changes[:applied_count])
        expected = self._state_after(originals, changes, applied_count)
        self._verify(expected, paths)
        # Restore originals first, then remove files created by the transaction.
        for path in sorted(paths):
            original = originals[path]
            if original is None:
                continue
            current = self._current(path)
            if current == original:
                continue
            target = self._path(path, internal=path.startswith(".obsai-trash/"))
            self.safe._atomic_write(
                target, original, expected_hash=_hash(current) if current is not None else None
            )
            mode = original_modes[path]
            if mode is not None:
                target.chmod(mode)
        for path in sorted(paths):
            if originals[path] is None and self._current(path) is not None:
                target = self._path(path, internal=path.startswith(".obsai-trash/"))
                target.unlink()
                _sync_directory(target.parent)
        for directory in sorted(absent_directories, key=lambda item: item.count("/"), reverse=True):
            relative = PurePosixPath(directory)
            target = self.root / directory
            if (relative.is_absolute() or any(part in (".", "..") for part in directory.split("/"))
                    or not target.resolve(strict=False).is_relative_to(self.root)):
                raise RecoveryRequiredError(f"Unsafe transaction directory: {directory}")
            try:
                target.rmdir()
            except OSError:
                pass

    def _cleanup(self, journal: TransactionJournal) -> None:
        try:
            shutil.rmtree(journal.directory)
        except OSError:
            return
        try:
            journal_base(self.root).rmdir()
        except OSError:
            pass

    def _reindex(self, plan: TransactionPlan) -> None:
        if self.indexer is not None:
            self.indexer(self.root)
        elif self.database_path is not None:
            from obsai.indexing import IncrementalIndexer
            from obsai.storage import Database, IndexRepository

            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with Database(self.database_path) as database:
                repository = IndexRepository(database)
                with database.transaction():
                    # Preserve logical identity even when a move also changes content.
                    for change in plan.changes:
                        if change.operation == "move" and change.destination is not None:
                            existing = repository.notes.get_by_path(change.path)
                            if existing is not None:
                                repository.notes.update_path(existing.id, change.destination)
                    IncrementalIndexer(repository).update(self.root)

    def _mark_index_dirty(self, paths: list[str], reason: str) -> None:
        if self.database_path is None:
            return
        try:
            from obsai.storage import Database, IndexRepository

            with Database(self.database_path) as database:
                repository = IndexRepository(database)
                for path in paths:
                    repository.mark_dirty(path, reason)
        except Exception:
            # The durable journal remains authoritative when SQLite itself is unavailable.
            pass

    def execute(self, plan: TransactionPlan, *, approved: bool) -> TransactionResult:
        if not approved:
            return TransactionResult(None, committed=False, cancelled=True)
        self.preflight(plan)
        transaction_id = uuid4().hex
        try:
            journal = TransactionJournal.create(self.root, transaction_id, plan)
        except OSError as exc:
            raise TransactionError(f"Cannot prepare transaction snapshots: {exc}") from exc
        applied_count = 0
        try:
            journal.update(status="applying")
            for change in plan.changes:
                self.safe.apply(ChangeSet(change), approved=True)
                applied_count += 1
                journal.update(applied_count=applied_count)
            self._verify(plan.finals)
            journal.update(status="committed")
        except Exception as exc:
            try:
                journal.update(status="rolling_back", error=f"{type(exc).__name__}: {exc}")
                self._rollback(
                    plan.originals, plan.original_modes, plan.changes, applied_count,
                    plan.absent_directories,
                )
                journal.update(status="rolled_back")
                self._cleanup(journal)
            except Exception as rollback_exc:
                journal.update(status="recovery_required", error=f"{type(rollback_exc).__name__}: {rollback_exc}")
                raise RecoveryRequiredError(
                    f"Rollback failed for {transaction_id}; run 'obsai transaction recover {transaction_id}'"
                ) from rollback_exc
            raise TransactionError(f"Transaction failed and was rolled back: {exc}") from exc

        try:
            self._reindex(plan)
        except Exception as exc:
            paths = sorted(self._changed_paths(plan.changes))
            reason = f"Index update failed after Vault commit: {type(exc).__name__}: {exc}"
            journal.update(status="index_dirty", dirty_paths=paths, error=reason)
            self._mark_index_dirty(paths, reason)
            return TransactionResult(transaction_id, committed=True, index_dirty=True, index_error=reason)
        journal.update(status="complete")
        self._cleanup(journal)
        return TransactionResult(transaction_id, committed=True)

    def recover(self, transaction_id: str, *, approved: bool) -> TransactionResult:
        if not approved:
            return TransactionResult(transaction_id, committed=False, cancelled=True)
        journal = TransactionJournal.load(self.root, transaction_id)
        if journal.data["status"] not in UNFINISHED:
            raise TransactionError(f"Transaction {transaction_id} is not awaiting Vault recovery")
        originals, changes, count = self._recovery_state(journal)
        original_modes = {item["path"]: item.get("mode") for item in journal.data["originals"]}
        journal.update(status="rolling_back")
        try:
            self._rollback(
                originals, original_modes, changes, count,
                journal.data.get("absent_directories", []),
            )
        except Exception as exc:
            journal.update(status="recovery_required", error=f"{type(exc).__name__}: {exc}")
            raise RecoveryRequiredError(f"Recovery failed; inspect {journal.directory}") from exc
        journal.update(status="rolled_back")
        self._cleanup(journal)
        return TransactionResult(transaction_id, committed=False)

    def _recovery_state(
        self, journal: TransactionJournal,
    ) -> tuple[dict[str, bytes | None], tuple[FileChange, ...], int]:
        originals = journal.originals()
        changes = tuple(FileChange(**item) for item in journal.data["changes"])
        actual = {path: self._current(path) for path in originals}
        matching = []
        for count in range(len(changes) + 1):
            if actual == self._state_after(originals, changes, count):
                matching.append(count)
        if not matching:
            raise RecoveryRequiredError(
                f"Files no longer match a known state for {journal.data['id']}; inspect {journal.directory}"
            )
        return originals, changes, max(matching)

    def preview_recovery(self, transaction_id: str, console: Console) -> None:
        """Show the exact current-to-snapshot diff before rollback approval."""
        journal = TransactionJournal.load(self.root, transaction_id)
        if journal.data["status"] not in UNFINISHED:
            raise TransactionError(f"Transaction {transaction_id} is not awaiting Vault recovery")
        originals, _, _ = self._recovery_state(journal)
        for path, original in sorted(originals.items()):
            current = self._current(path)
            if current == original:
                continue
            old_lines = (current or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
            new_lines = (original or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
            for line in difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{path}" if current is not None else "/dev/null",
                tofile=f"b/{path}" if original is not None else "/dev/null",
            ):
                style = "green" if line.startswith("+") else "red" if line.startswith("-") else "cyan" if line.startswith("@@") else None
                console.print(line.rstrip("\n"), style=style, markup=False, highlight=False)

    def clear_index_dirty(self) -> None:
        """Call after a successful full incremental update."""
        for item in list_journals(self.root):
            if item["status"] in ("index_dirty", "committed"):
                if self.database_path is not None:
                    from obsai.storage import Database, IndexRepository

                    with Database(self.database_path) as database:
                        repository = IndexRepository(database)
                        for path in item.get("dirty_paths", []):
                            repository.clear_dirty(path)
                self._cleanup(TransactionJournal.load(self.root, item["id"]))
