from pathlib import Path

import pytest

from obsai.config.loader import default_config_path, load_settings
from obsai.errors import ConfigError


def test_defaults_do_not_create_config(tmp_path: Path) -> None:
    assert default_config_path() == tmp_path / "config" / "obsai" / "config.toml"
    settings = load_settings()
    assert settings.vault.path is None
    assert settings.index.database is None
    assert settings.embedding.provider == "openai"
    assert settings.embedding.model == "text-embedding-3-small"
    assert settings.embedding.batch_size == 64
    assert not (tmp_path / "config").exists()


def test_load_config_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[vault]\npath = "/tmp/vault"\n[index]\ndatabase = "/tmp/index.db"\n')
    settings = load_settings(config)
    assert settings.vault.path == Path("/tmp/vault")
    assert settings.index.database == Path("/tmp/index.db")


@pytest.mark.parametrize("content", ['[vault]\npath = 42\n', '[vault]\nunknown = "x"\n', '[vault\n'])
def test_invalid_config_raises_config_error(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(content)
    with pytest.raises(ConfigError):
        load_settings(config)


def test_explicit_missing_config_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.toml")


def test_environment_is_isolated_and_can_supply_a_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OBSAI_VAULT__PATH", str(tmp_path / "vault"))
    settings = load_settings()
    assert settings.vault.path == tmp_path / "vault"
    assert settings.index.database is None
    assert not (tmp_path / "config").exists()


def test_environment_overrides_file_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[vault]\npath = "/tmp/from-file"\n')
    monkeypatch.setenv("OBSAI_VAULT__PATH", str(tmp_path / "from-env"))
    assert load_settings(config).vault.path == tmp_path / "from-env"


def test_home_fallback_uses_isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert default_config_path() == tmp_path / ".config" / "obsai" / "config.toml"


def test_embedding_config_and_budget_validation(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[embedding]\nmodel_version = "revision-2"\n'
        'batch_size = 8\nmax_embedding_tokens = 1000\n', encoding="utf-8"
    )
    settings = load_settings(config)
    assert settings.embedding.model_version == "revision-2"
    assert settings.embedding.batch_size == 8
    assert settings.embedding.max_embedding_tokens == 1000
    config.write_text('[embedding]\nbatch_size = 0\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_settings(config)
