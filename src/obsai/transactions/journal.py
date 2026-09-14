"""Durable, inspectable transaction journal and short-lived byte snapshots."""

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from obsai.errors import RecoveryRequiredError, TransactionError
from obsai.safe_write.service import _hash, _sync_directory
from obsai.transactions.models import TransactionPlan

JOURNAL_DIR = ".obsai-transactions"
UNFINISHED = frozenset({"prepared", "applying", "rolling_back", "recovery_required"})
KNOWN_STATUSES = UNFINISHED | {"committed", "index_dirty", "complete", "rolled_back"}


def journal_base(root: Path) -> Path:
    base = root / JOURNAL_DIR
    if base.is_symlink():
        raise TransactionError("Transaction journal directory must not be a symlink")
    return base


def list_journals(root: Path) -> list[dict]:
    base = journal_base(root)
    if not base.exists():
        return []
    if not base.is_dir():
        raise RecoveryRequiredError(f"Transaction journal path is not a directory: {base}")
    journals = []
    for directory in sorted(base.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            raise RecoveryRequiredError(f"Unsafe transaction journal entry: {directory}")
        path = directory / "journal.json"
        if not path.is_file() or path.is_symlink():
            raise RecoveryRequiredError(f"Transaction journal missing or unsafe: {directory}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryRequiredError(f"Cannot read transaction journal: {path}") from exc
        if data.get("id") != directory.name or data.get("status") not in KNOWN_STATUSES:
            raise RecoveryRequiredError(f"Invalid transaction journal: {path}")
        journals.append(data)
    return journals


class TransactionJournal:
    def __init__(self, root: Path, transaction_id: str):
        if not transaction_id or any(char not in "0123456789abcdef" for char in transaction_id):
            raise TransactionError("Invalid transaction ID")
        self.root = root
        self.directory = journal_base(root) / transaction_id
        self.path = self.directory / "journal.json"
        self.data: dict = {}

    @classmethod
    def create(cls, root: Path, transaction_id: str, plan: TransactionPlan) -> "TransactionJournal":
        journal = cls(root, transaction_id)
        journal.directory.mkdir(parents=True, exist_ok=False)
        try:
            snapshot_dir = journal.directory / "snapshots"
            snapshot_dir.mkdir()
            originals = []
            for number, (path, content) in enumerate(sorted(plan.originals.items())):
                snapshot = None
                if content is not None:
                    snapshot = f"snapshots/{number}.bin"
                    with (journal.directory / snapshot).open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                originals.append({
                    "path": path, "hash": _hash(content) if content is not None else None,
                    "snapshot": snapshot, "mode": plan.original_modes[path],
                })
            _sync_directory(snapshot_dir)
            journal.data = {
                "id": transaction_id, "status": "prepared", "applied_count": 0,
                "originals": originals,
                "changes": [asdict(change) for change in plan.changes],
                "absent_directories": list(plan.absent_directories),
                "dirty_paths": [], "error": None,
            }
            journal.save()
        except Exception:
            shutil.rmtree(journal.directory, ignore_errors=True)
            raise
        return journal

    @classmethod
    def load(cls, root: Path, transaction_id: str) -> "TransactionJournal":
        journal = cls(root, transaction_id)
        if journal.directory.is_symlink() or journal.path.is_symlink():
            raise RecoveryRequiredError(f"Unsafe transaction journal: {journal.path}")
        try:
            journal.data = json.loads(journal.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryRequiredError(f"Cannot read transaction journal: {journal.path}") from exc
        if journal.data.get("id") != transaction_id or journal.data.get("status") not in KNOWN_STATUSES:
            raise RecoveryRequiredError(f"Invalid transaction journal: {journal.path}")
        return journal

    def save(self) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=".journal-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.data, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _sync_directory(self.directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def update(self, *, status: str | None = None, applied_count: int | None = None,
               error: str | None = None, dirty_paths: list[str] | None = None) -> None:
        if status is not None:
            self.data["status"] = status
        if applied_count is not None:
            self.data["applied_count"] = applied_count
        if error is not None:
            self.data["error"] = error
        if dirty_paths is not None:
            self.data["dirty_paths"] = dirty_paths
        self.save()

    def originals(self) -> dict[str, bytes | None]:
        states = {}
        for item in self.data["originals"]:
            snapshot = item["snapshot"]
            if snapshot is not None:
                candidate = self.directory / snapshot
                parts = Path(snapshot).parts
                if (len(parts) != 2 or parts[0] != "snapshots" or not parts[1].endswith(".bin")
                        or not parts[1][:-4].isdigit() or candidate.is_symlink()
                        or candidate.parent.is_symlink()):
                    raise RecoveryRequiredError(f"Unsafe transaction snapshot: {snapshot}")
                content = candidate.read_bytes()
            else:
                content = None
            if (None if content is None else _hash(content)) != item["hash"]:
                raise RecoveryRequiredError(f"Transaction snapshot is corrupt: {item['path']}")
            states[item["path"]] = content
        return states
