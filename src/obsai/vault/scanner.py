"""Deterministic vault scan, excluding hidden, symlinked, and ignored notes."""

import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from obsai.errors import VaultError
from obsai.vault.models import ParsedNote
from obsai.vault.parser import parse_note


def _vault_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"Cannot access vault {root}: {exc}") from exc
    if not resolved.is_dir():
        raise VaultError(f"Vault is not a directory: {root}")
    return resolved


def _ignore_spec(root: Path) -> GitIgnoreSpec:
    ignore_file = root / ".obsaiignore"
    if ignore_file.is_symlink():
        raise VaultError(f"Ignore file must not be a symlink: {ignore_file}")
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError) as exc:
        raise VaultError(f"Cannot read {ignore_file}: {exc}") from exc
    try:
        return GitIgnoreSpec.from_lines(lines)
    except ValueError as exc:
        raise VaultError(f"Invalid {ignore_file}: {exc}") from exc


def scan_markdown_files(root: Path) -> list[Path]:
    """Return sorted readable Markdown paths; never descend into excluded dirs."""
    root = _vault_root(root)
    ignore = _ignore_spec(root)
    paths: list[Path] = []

    def on_error(exc: OSError) -> None:
        raise VaultError(f"Cannot scan vault {root}: {exc}") from exc

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
        directory = Path(parent)
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and not (directory / name).is_symlink()
            and not ignore.match_file((directory / name).relative_to(root).as_posix() + "/")
        )
        for name in sorted(files):
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            path = directory / name
            if path.is_symlink() or ignore.match_file(path.relative_to(root).as_posix()):
                continue
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def parse_vault(root: Path) -> list[ParsedNote]:
    """Parse only the files admitted by the scanner."""
    root = _vault_root(root)
    return [parse_note(path, vault_root=root) for path in scan_markdown_files(root)]
