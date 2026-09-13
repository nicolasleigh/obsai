from pathlib import Path

from typer.testing import CliRunner

from obsai.cli.app import app
from obsai.storage import Database, IndexRepository

runner = CliRunner()


def test_index_update_cli_reports_counts_and_wikilink_impact(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "A.md"
    target.write_text("# Stable Title\n\nBody.\n", encoding="utf-8")
    (vault / "Reference.md").write_text("# Reference\n\n[[A]]\n", encoding="utf-8")
    database_path = tmp_path / "metadata.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )

    first = runner.invoke(app, ["index", "update"])
    assert first.exit_code == 0, first.output
    assert "Created: 2" in first.output
    assert "Modified: 0" in first.output
    assert "Unchanged: 0" in first.output
    with Database(database_path) as database:
        note_id = IndexRepository(database).notes.get_by_path("A.md").id

    second = runner.invoke(app, ["index", "update"])
    assert second.exit_code == 0, second.output
    assert "Unchanged: 2" in second.output

    target.rename(vault / "B.md")
    renamed = runner.invoke(app, ["index", "update"])
    assert renamed.exit_code == 0, renamed.output
    assert "Renamed: 1" in renamed.output
    assert "Affected WikiLinks: 1" in renamed.output
    assert "A.md → B.md" in renamed.output
    assert "Reference.md" in renamed.output
    with Database(database_path) as database:
        assert IndexRepository(database).notes.get_by_path("B.md").id == note_id


def test_index_update_uses_isolated_default_database_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")

    result = runner.invoke(app, ["index", "update"])
    assert result.exit_code == 0, result.output
    assert "Created: 1" in result.output
    assert (tmp_path / ".obsai" / "index.db").is_file()
