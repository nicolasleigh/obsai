from pathlib import Path

from typer.testing import CliRunner

from obsai.cli.app import app


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")
    return vault


def test_cli_rejection_leaves_vault_byte_for_byte_unchanged(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    original = b"# Title\r\n\r\nOld text.\r\n"
    (vault / "A.md").write_bytes(original)
    result = CliRunner().invoke(
        app, ["note", "update", "A.md", "--old", "Old", "--new", "New"], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "-Old text." in result.output
    assert "+New text." in result.output
    assert "Cancelled" in result.output
    assert (vault / "A.md").read_bytes() == original
    assert [path.name for path in vault.iterdir()] == ["A.md"]


def test_cli_approves_create_update_frontmatter_move_and_trash(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    runner = CliRunner()
    created = runner.invoke(
        app, ["note", "create", "A.md", "--content", "# A\nOld"], input="y\n"
    )
    assert created.exit_code == 0, created.output
    assert (vault / "A.md").is_file()
    updated = runner.invoke(
        app, ["note", "update", "A.md", "--old", "Old", "--new", "New"], input="y\n"
    )
    assert updated.exit_code == 0, updated.output
    frontmatter = runner.invoke(
        app, ["note", "frontmatter", "A.md", "--set", "status=done"], input="y\n"
    )
    assert frontmatter.exit_code == 0, frontmatter.output
    assert "status: done" in (vault / "A.md").read_text(encoding="utf-8")
    (vault / "Ref.md").write_bytes(b"[[A]]\n")
    moved = runner.invoke(app, ["note", "move", "A.md", "B.md"], input="y\n")
    assert moved.exit_code == 0, moved.output
    assert "Affected backlinks (1)" in moved.output
    assert (vault / "B.md").is_file()
    trashed = runner.invoke(app, ["note", "trash", "B.md"], input="y\n")
    assert trashed.exit_code == 0, trashed.output
    assert not (vault / "B.md").exists()
    assert len(list((vault / ".obsai-trash").rglob("B.md"))) == 1
