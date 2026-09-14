"""Build a fresh derived index beside the live one, then atomically replace it."""

import fcntl
import os
from pathlib import Path

from obsai.errors import SchemaError
from obsai.indexing.incremental import IncrementalIndexer, UpdateResult
from obsai.safe_write.service import _sync_directory
from obsai.shutdown import check_shutdown, defer_shutdown
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionService
from obsai.vault.scanner import scan_markdown_files


class ShadowIndexRebuilder:
    def __init__(self, vault_root: Path, database_path: Path):
        self.vault = vault_root.expanduser().resolve(strict=True)
        self.database_path = database_path.expanduser()
        self.shadow_path = self.database_path.with_name(self.database_path.name + ".building")
        self.lock_path = self.database_path.with_name(self.database_path.name + ".building.lock")

    @staticmethod
    def _remove_derived(path: Path) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(path) + suffix)
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise SchemaError(f"Unsafe shadow index artifact: {candidate}")
            candidate.unlink(missing_ok=True)

    def rebuild(self) -> UpdateResult:
        TransactionService(self.vault).ensure_ready()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.is_symlink() or (self.database_path.exists() and not self.database_path.is_file()):
            raise SchemaError(f"Unsafe live index path: {self.database_path}")
        original_stat = self.database_path.stat() if self.database_path.exists() else None
        original_fingerprint = (
            (original_stat.st_dev, original_stat.st_ino, original_stat.st_size,
             original_stat.st_mtime_ns) if original_stat is not None else None
        )
        for suffix in ("-wal", "-shm"):
            if Path(str(self.database_path) + suffix).exists():
                raise SchemaError("Live index has WAL sidecars; close other index connections before rebuild")
        if self.lock_path.is_symlink():
            raise SchemaError(f"Unsafe shadow index lock: {self.lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SchemaError("Another index rebuild is already running") from exc
            self._remove_derived(self.shadow_path)
            try:
                check_shutdown()
                with Database(self.shadow_path) as shadow:
                    result = IncrementalIndexer(IndexRepository(shadow)).update(self.vault)
                    check_shutdown()
                    integrity = shadow.connection.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise SchemaError(f"Shadow index integrity check failed: {integrity}")
                    if shadow.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise SchemaError("Shadow index foreign key check failed")
                    note_count = shadow.connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                    chunk_count = shadow.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                    fts_count = shadow.connection.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
                    if note_count != len(scan_markdown_files(self.vault)) or chunk_count != fts_count:
                        raise SchemaError("Shadow index validation counts do not match the Vault or FTS")
                check_shutdown()
                current_stat = self.database_path.stat() if self.database_path.exists() else None
                current_fingerprint = (
                    (current_stat.st_dev, current_stat.st_ino, current_stat.st_size,
                     current_stat.st_mtime_ns) if current_stat is not None else None
                )
                if current_fingerprint != original_fingerprint:
                    raise SchemaError("Live index changed during rebuild; retry after other writers stop")
                # A signal in this tiny critical region is deferred until the new,
                # already validated index is durable. Before it, the old file remains.
                with defer_shutdown():
                    os.replace(self.shadow_path, self.database_path)
                    _sync_directory(self.database_path.parent)
                check_shutdown()
                return result
            finally:
                self._remove_derived(self.shadow_path)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
