import sys

import pytest
from typer.testing import CliRunner

from obsai.cli.app import app, main

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "obsai 0.1.0" in result.stdout


def test_status_without_config() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "ObsAgent initialized" in result.stdout
    assert "No vault configured" in result.stdout


def test_console_script_reports_invalid_config(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[vault]\npath = 42\n')
    monkeypatch.setattr(sys, "argv", ["obsai", "status"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "Invalid configuration" in capsys.readouterr().err
