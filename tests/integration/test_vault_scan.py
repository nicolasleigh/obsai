from pathlib import Path

import pytest

from obsai.errors import VaultError
from obsai.vault import parse_vault, scan_markdown_files

VAULT = Path(__file__).parents[1] / "fixtures" / "vault"


def test_scan_and_parse_exclude_ignored_files_before_reading() -> None:
    paths = [path.relative_to(VAULT).as_posix() for path in scan_markdown_files(VAULT)]
    assert paths == [
        "basic.md",
        "blocks.md",
        "callout.md",
        "code.md",
        "dataview.md",
        "frontmatter.md",
        "nested/child.md",
        "wikilinks.md",
    ]
    notes = parse_vault(VAULT)
    assert [note.path for note in notes] == paths


def test_scan_skips_hidden_and_symlinked_files(tmp_path: Path) -> None:
    (tmp_path / "visible.md").write_text("# Visible\n")
    (tmp_path / ".hidden.md").write_text("# Hidden\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "inside.md").write_text("# Hidden\n")
    (tmp_path / "linked.md").symlink_to(tmp_path / "visible.md")
    assert [path.name for path in scan_markdown_files(tmp_path)] == ["visible.md"]


def test_ignored_directory_is_never_read(tmp_path: Path) -> None:
    (tmp_path / ".obsaiignore").write_text("Private/\n")
    private = tmp_path / "Private"
    private.mkdir()
    (private / "bad.md").write_text("---\nbroken: [\n")
    (tmp_path / "good.md").write_text("# Good\n")
    assert [note.path for note in parse_vault(tmp_path)] == ["good.md"]


def test_symlinked_ignore_file_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "rules.txt"
    external.write_text("Private/\n")
    (tmp_path / ".obsaiignore").symlink_to(external)
    with pytest.raises(VaultError, match="symlink"):
        scan_markdown_files(tmp_path)
