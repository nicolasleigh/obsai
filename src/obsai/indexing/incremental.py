"""Hash-verified incremental index with conservative exact rename matching."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from obsai.chunking import chunk_note
from obsai.errors import VaultError
from obsai.storage import IndexRepository, LinkImpact
from obsai.vault import scan_markdown_files
from obsai.vault.models import ParsedNote
from obsai.vault.parser import parse_markdown

ChangeKind = Literal["unchanged", "created", "modified", "deleted", "renamed", "moved"]


@dataclass(frozen=True)
class FileChange:
    kind: ChangeKind
    path: str
    note_id: str | None = None
    old_path: str | None = None
    affected_links: tuple[LinkImpact, ...] = ()


@dataclass(frozen=True)
class UpdateResult:
    changes: tuple[FileChange, ...]

    def count(self, kind: ChangeKind) -> int:
        return sum(change.kind == kind for change in self.changes)

    @property
    def affected_link_count(self) -> int:
        return sum(len(change.affected_links) for change in self.changes)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_note(path: Path) -> str:
    if path.is_symlink():
        raise VaultError(f"Refusing to read symlinked note: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VaultError(f"Cannot read note {path}: {exc}") from exc


class IncrementalIndexer:
    def __init__(self, repository: IndexRepository):
        self.repository = repository

    def update(self, vault_root: Path) -> UpdateResult:
        """Read the Vault as truth, then apply all derived DB changes atomically."""
        files = scan_markdown_files(vault_root)
        root = vault_root.resolve(strict=True)
        previous = {record.path: record for record in self.repository.notes.list_index_facts()}
        current_paths: set[str] = set()
        new_hashes: dict[str, str] = {}
        modified_hashes: dict[str, str] = {}
        unchanged: list[FileChange] = []

        for path in files:
            relative = path.relative_to(root).as_posix()
            current_paths.add(relative)
            content_hash = _hash(_read_note(path))
            record = previous.get(relative)
            if record is None:
                new_hashes[relative] = content_hash
            elif content_hash == record.content_hash:
                unchanged.append(FileChange("unchanged", relative, note_id=record.id))
            else:
                modified_hashes[relative] = content_hash

        disappeared = {path: record for path, record in previous.items() if path not in current_paths}
        old_by_hash: dict[str, list[str]] = defaultdict(list)
        new_by_hash: dict[str, list[str]] = defaultdict(list)
        for path, record in disappeared.items():
            old_by_hash[record.content_hash].append(path)
        for path, content_hash in new_hashes.items():
            new_by_hash[content_hash].append(path)

        moves: list[FileChange] = []
        move_hashes: dict[str, str] = {}
        for content_hash, old_paths in old_by_hash.items():
            new_paths = new_by_hash.get(content_hash, [])
            # An ambiguous match may be a copy; do not silently choose an ID.
            if len(old_paths) != 1 or len(new_paths) != 1:
                continue
            old_path, new_path = old_paths[0], new_paths[0]
            record = disappeared.pop(old_path)
            new_hashes.pop(new_path)
            move_hashes[new_path] = content_hash
            kind: ChangeKind = (
                "renamed"
                if PurePosixPath(old_path).parent == PurePosixPath(new_path).parent
                else "moved"
            )
            impacts = tuple(self.repository.backlinks_for_path(record.id, old_path))
            moves.append(FileChange(kind, new_path, record.id, old_path, impacts))

        def parse_changed(path: str, expected_hash: str) -> ParsedNote:
            content = _read_note(root / path)
            if _hash(content) != expected_hash:
                raise VaultError(f"Note changed during index update: {path}")
            return parse_markdown(content, path)

        changes: list[FileChange] = [*unchanged]
        # Parsing and all DB changes share one transaction. A failure rolls back
        # earlier note writes without retaining the entire Vault in memory.
        with self.repository.db.transaction():
            for path, record in sorted(disappeared.items()):
                if (root / path).exists():
                    raise VaultError(f"Note reappeared during index update: {path}")
                self.repository.notes.delete(record.id)
                self.repository.clear_dirty(path)
                changes.append(FileChange("deleted", path, note_id=record.id))
            for move in sorted(moves, key=lambda item: item.path):
                if _hash(_read_note(root / move.path)) != move_hashes[move.path]:
                    raise VaultError(f"Note changed during index update: {move.path}")
                assert move.note_id is not None
                self.repository.notes.update_path(move.note_id, move.path)
                self.repository.clear_dirty(move.path)
                changes.append(move)
            for path in sorted(modified_hashes):
                parsed = parse_changed(path, modified_hashes[path])
                note_id = self.repository.index_note(
                    parsed, chunk_note(parsed), reconcile=False
                )
                changes.append(FileChange("modified", path, note_id=note_id))
            for path in sorted(new_hashes):
                parsed = parse_changed(path, new_hashes[path])
                note_id = self.repository.index_note(
                    parsed, chunk_note(parsed), reconcile=False
                )
                changes.append(FileChange("created", path, note_id=note_id))
            self.repository.reconcile_links(full=bool(moves or disappeared or new_hashes))
            self.repository.set_state("last_update", datetime.now(timezone.utc).isoformat())
        return UpdateResult(tuple(sorted(changes, key=lambda item: (item.path, item.kind))))
