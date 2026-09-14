"""Previewed, OCC-checked, single-note Vault writes."""

import difflib
import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4
from typing import Any, Mapping

import yaml
from rich.console import Console

from obsai.errors import CollisionError, ConflictError, InvalidEncodingError, SafeWriteError, VaultError
from obsai.safe_write.models import ChangeSet, FileChange
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(value: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise InvalidEncodingError("New note content is not valid UTF-8") from exc


def _sync_directory(path: Path) -> None:
    """Best-effort directory durability; unavailable on some platforms."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


class SafeWriteService:
    def __init__(self, vault_root: Path):
        try:
            self.root = vault_root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise VaultError(f"Cannot access vault {vault_root}: {exc}") from exc
        if not self.root.is_dir():
            raise VaultError(f"Vault is not a directory: {vault_root}")

    def _path(self, relative: str, *, internal: bool = False) -> Path:
        if not relative or "\\" in relative or "\x00" in relative:
            raise SafeWriteError("Use a nonempty Vault-relative POSIX Markdown path")
        candidate = PurePosixPath(relative)
        if candidate.is_absolute() or any(part in (".", "..") for part in relative.split("/")):
            raise SafeWriteError(f"Path escapes or is not relative to the Vault: {relative}")
        if candidate.suffix.lower() != ".md":
            raise SafeWriteError("Only Markdown .md notes may be changed")
        if not internal and candidate.parts[0] == ".obsai-trash":
            raise SafeWriteError("The Vault trash directory is reserved")
        path = self.root.joinpath(*candidate.parts)
        cursor = self.root
        for part in candidate.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SafeWriteError(f"Symlinked Vault paths are not writable: {relative}")
        if not path.resolve(strict=False).is_relative_to(self.root):
            raise SafeWriteError(f"Path escapes the Vault: {relative}")
        return path

    def _read(self, path: Path) -> tuple[str, str]:
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SafeWriteError(f"Not a regular note: {path.relative_to(self.root)}")
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot read note {path}: {exc}") from exc
        try:
            return data.decode("utf-8", errors="strict"), _hash(data)
        except UnicodeError as exc:
            raise InvalidEncodingError(f"Note is not UTF-8: {path.relative_to(self.root)}") from exc

    def _vacant(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            raise CollisionError(f"Destination already exists: {path.relative_to(self.root)}")
        if path.parent.exists() and not path.parent.is_dir():
            raise SafeWriteError(f"Destination parent is not a directory: {path.parent}")

    def create_note(self, path: str, content: str) -> ChangeSet:
        target = self._path(path)
        self._vacant(target)
        _encode(content)
        return ChangeSet(FileChange("create", path, None, None, None, content))

    def update_note(self, path: str, old: str, replacement: str) -> ChangeSet:
        """Replace one exact span, avoiding a model-authored whole-note overwrite."""
        target = self._path(path)
        content, original_hash = self._read(target)
        if not old or content.count(old) != 1:
            raise SafeWriteError("The old text must occur exactly once")
        updated = content.replace(old, replacement, 1)
        if updated == content:
            raise SafeWriteError("Replacement does not change the note")
        _encode(updated)
        return ChangeSet(FileChange("update", path, None, original_hash, content, updated))

    def update_frontmatter(self, path: str, updates: Mapping[str, Any]) -> ChangeSet:
        target = self._path(path)
        content, original_hash = self._read(target)
        if not updates or any(not isinstance(key, str) or not key for key in updates):
            raise SafeWriteError("Frontmatter updates require nonempty string keys")
        lines = content.splitlines(keepends=True)
        end = None
        body = content
        metadata: dict[str, Any] = {}
        if lines and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
            if end is None:
                raise SafeWriteError("Unclosed YAML frontmatter")
            try:
                loaded = yaml.safe_load("".join(lines[1:end]))
            except yaml.YAMLError as exc:
                raise SafeWriteError(f"Invalid YAML frontmatter: {exc}") from exc
            if loaded is not None:
                if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
                    raise SafeWriteError("Frontmatter must be a mapping with string keys")
                metadata = loaded
            body = "".join(lines[end + 1 :])
        metadata.update(updates)
        try:
            header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        except yaml.YAMLError as exc:
            raise SafeWriteError(f"Cannot serialize frontmatter: {exc}") from exc
        updated = f"---\n{header}---\n{body}"
        _encode(updated)
        return ChangeSet(FileChange("frontmatter", path, None, original_hash, content, updated))

    def _backlink_impact(self, path: str) -> tuple[str, ...]:
        old = PurePosixPath(path)
        old_without_suffix = str(old.with_suffix(""))
        affected: set[str] = set()
        for candidate in scan_markdown_files(self.root):
            source = candidate.relative_to(self.root).as_posix()
            if source == path:
                continue
            note = parse_note(candidate, vault_root=self.root)
            for link in note.wikilinks:
                target = link.target_path
                if not target:
                    continue
                normalized = target[:-3] if target.lower().endswith(".md") else target
                if normalized in (old_without_suffix, old.stem):
                    affected.add(source)
                    break
                relative_target = str(PurePosixPath(source).parent / normalized)
                if relative_target == old_without_suffix:
                    affected.add(source)
                    break
        return tuple(sorted(affected))

    def move_note(self, path: str, destination: str) -> ChangeSet:
        source = self._path(path)
        target = self._path(destination)
        if source == target:
            raise SafeWriteError("Source and destination are identical")
        content, original_hash = self._read(source)
        self._vacant(target)
        impacts = self._backlink_impact(path)
        return ChangeSet(FileChange("move", path, destination, original_hash, content, content, impacts))

    def trash_note(self, path: str) -> ChangeSet:
        source = self._path(path)
        content, original_hash = self._read(source)
        destination = f".obsai-trash/{uuid4().hex}/{path}"
        self._vacant(self._path(destination, internal=True))
        return ChangeSet(FileChange("trash", path, destination, original_hash, content, None))

    def preview(self, change: ChangeSet, console: Console) -> None:
        file = change.file
        old_label = f"a/{file.path}" if file.original_content is not None else "/dev/null"
        new_label = (
            f"b/{file.destination or file.path}" if file.operation != "trash" else "/dev/null"
        )
        if file.operation == "move":
            lines = [f"--- {old_label}\n", f"+++ {new_label}\n", "(content unchanged; path moves)\n"]
        else:
            lines = list(difflib.unified_diff(
                (file.original_content or "").splitlines(keepends=True),
                (file.new_content or "").splitlines(keepends=True),
                fromfile=old_label, tofile=new_label,
            ))
        if not lines:
            lines = [f"--- {old_label}\n", f"+++ {new_label}\n", "(empty content)\n"]
        for line in lines:
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else "cyan" if line.startswith("@@") else None
            console.print(line.rstrip("\n"), style=style, markup=False, highlight=False)
        if file.operation == "trash":
            console.print(f"Trash destination: {file.destination}", style="yellow", markup=False)
        if file.affected_backlinks:
            console.print(f"Affected backlinks ({len(file.affected_backlinks)}); they will not be rewritten:", style="yellow")
            for source in file.affected_backlinks:
                console.print(f"  {source}", markup=False)

    def _atomic_write(self, path: Path, data: bytes, *, expected_hash: str | None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".obsai-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                if expected_hash is not None:
                    try:
                        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
                    except FileNotFoundError as exc:
                        raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
                os.fsync(handle.fileno())
            relative = path.relative_to(self.root)
            self._path(relative.as_posix(), internal=relative.parts[0] == ".obsai-trash")
            self._check_current(path, expected_hash)
            if expected_hash is None:
                # Atomic no-clobber create; unlike replace, this cannot overwrite a racing file.
                os.link(temporary, path)
                temporary.unlink()
            else:
                os.replace(temporary, path)
            _sync_directory(path.parent)
        except FileExistsError as exc:
            raise CollisionError(f"Destination already exists: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot commit note {path.relative_to(self.root)}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _check_current(self, path: Path, expected_hash: str | None) -> None:
        if expected_hash is None:
            self._vacant(path)
            return
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ConflictError(f"Note is no longer a regular file: {path.relative_to(self.root)}")
            current = path.read_bytes()
        except FileNotFoundError as exc:
            raise ConflictError(f"Note disappeared: {path.relative_to(self.root)}") from exc
        except OSError as exc:
            raise SafeWriteError(f"Cannot recheck note {path.relative_to(self.root)}: {exc}") from exc
        if _hash(current) != expected_hash:
            raise ConflictError(f"Note changed since preview: {path.relative_to(self.root)}")

    def apply(self, change: ChangeSet, *, approved: bool) -> bool:
        """No mutation occurs without explicit approval; recheck paths and hashes at commit."""
        if not approved:
            return False
        file = change.file
        source = self._path(file.path)
        if file.operation == "create":
            if file.original_hash is not None or file.new_content is None:
                raise SafeWriteError("Invalid create proposal")
            self._check_current(source, None)
            self._atomic_write(source, _encode(file.new_content), expected_hash=None)
        elif file.operation in ("update", "frontmatter"):
            if file.original_hash is None or file.new_content is None:
                raise SafeWriteError("Invalid update proposal")
            self._check_current(source, file.original_hash)
            self._atomic_write(source, _encode(file.new_content), expected_hash=file.original_hash)
        elif file.operation in ("move", "trash"):
            if file.original_hash is None or file.destination is None:
                raise SafeWriteError("Invalid move proposal")
            destination = self._path(file.destination, internal=file.operation == "trash")
            self._check_current(source, file.original_hash)
            self._vacant(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._path(file.destination, internal=file.operation == "trash")
            self._check_current(source, file.original_hash)
            try:
                os.link(source, destination)
            except FileExistsError as exc:
                raise CollisionError(f"Destination already exists: {file.destination}") from exc
            except OSError as exc:
                raise SafeWriteError(f"Cannot move note to {file.destination}: {exc}") from exc
            try:
                source.unlink()
            except OSError:
                destination.unlink(missing_ok=True)
                raise
            _sync_directory(source.parent)
            _sync_directory(destination.parent)
        else:
            raise SafeWriteError(f"Unknown operation: {file.operation}")
        return True
