"""Immutable single-file change proposals."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FileChange:
    operation: Literal["create", "update", "move", "trash", "frontmatter"]
    path: str
    destination: str | None
    original_hash: str | None
    original_content: str | None
    new_content: str | None
    affected_backlinks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeSet:
    file: FileChange
