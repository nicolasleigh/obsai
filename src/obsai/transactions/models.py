"""Plain, provider-independent transaction contracts."""

from dataclasses import dataclass, field
from typing import Any, Literal

from obsai.safe_write.models import FileChange


@dataclass(frozen=True)
class TransactionOperation:
    kind: Literal["create", "replace", "append", "frontmatter", "move", "trash", "rewrite_backlinks"]
    path: str
    destination: str | None = None
    old: str | None = None
    new: str | None = None
    updates: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, path: str, content: str) -> "TransactionOperation":
        return cls("create", path, new=content)

    @classmethod
    def replace(cls, path: str, old: str, new: str) -> "TransactionOperation":
        return cls("replace", path, old=old, new=new)

    @classmethod
    def append(cls, path: str, content: str) -> "TransactionOperation":
        return cls("append", path, new=content)

    @classmethod
    def frontmatter(cls, path: str, updates: dict[str, Any]) -> "TransactionOperation":
        return cls("frontmatter", path, updates=updates)

    @classmethod
    def move(cls, path: str, destination: str) -> "TransactionOperation":
        return cls("move", path, destination=destination)

    @classmethod
    def trash(cls, path: str) -> "TransactionOperation":
        return cls("trash", path)


@dataclass(frozen=True)
class TransactionPlan:
    vault_root: str
    operations: tuple[TransactionOperation, ...]
    changes: tuple[FileChange, ...]
    # Bytes are kept in memory only until the journal snapshot is durable.
    originals: dict[str, bytes | None]
    finals: dict[str, bytes | None]
    original_modes: dict[str, int | None]
    absent_directories: tuple[str, ...] = ()
    ambiguous_backlinks: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransactionResult:
    transaction_id: str | None
    committed: bool
    cancelled: bool = False
    index_dirty: bool = False
    index_error: str | None = None
