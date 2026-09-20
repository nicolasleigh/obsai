from pathlib import Path

import pytest

from obsai.config.loader import default_config_path, discover_config_path, load_settings
from obsai.errors import ConfigError


def test_defaults_do_not_create_config(tmp_path: Path) -> None:
    assert default_config_path() == tmp_path / "config" / "obsai" / "config.toml"
    settings = load_settings()
    assert settings.vault.path is None
    assert settings.index.database is None
    assert settings.embedding.provider == "openai"
    assert settings.embedding.model == "text-embedding-3-small"
    assert settings.embedding.batch_size == 64
    assert settings.ask.max_chunks == 6
    assert settings.organize.inbox == "Inbox"
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


def test_ask_budget_configuration_and_validation(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[ask]\nmax_context_tokens = 900\nmax_evidence_tokens = 150\nmax_chunks = 2\n',
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.ask.max_context_tokens == 900
    assert settings.ask.max_evidence_tokens == 150
    assert settings.ask.max_chunks == 2
    config.write_text('[ask]\nmax_chunks = 0\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_settings(config)


def test_organize_inbox_configuration_rejects_traversal(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[organize]\ninbox = "Capture"\n')
    assert load_settings(config).organize.inbox == "Capture"
    for invalid in ("../outside", "/absolute", "Inbox/../../outside"):
        config.write_text(f'[organize]\ninbox = "{invalid}"\n')
        with pytest.raises(ConfigError, match="Invalid configuration"):
            load_settings(config)


def test_discover_config_from_cwd_obsai_toml(tmp_path: Path) -> None:
    local_config = tmp_path / "obsai.toml"
    local_config.write_text('[vault]\npath = "/tmp/cwd-vault"\n')
    path, exists = discover_config_path(tmp_path)
    assert exists is True
    assert path == local_config
    settings = load_settings()
    assert settings.vault.path == Path("/tmp/cwd-vault")


def test_discover_config_from_cwd_dot_obsai(tmp_path: Path) -> None:
    dot_obsai = tmp_path / ".obsai"
    dot_obsai.mkdir()
    config = dot_obsai / "config.toml"
    config.write_text('[vault]\npath = "/tmp/dot-obsai-vault"\n')
    path, exists = discover_config_path(tmp_path)
    assert exists is True
    assert path == config
    settings = load_settings()
    assert settings.vault.path == Path("/tmp/dot-obsai-vault")


def test_discover_config_priority_obsai_toml_over_dot_obsai(tmp_path: Path) -> None:
    local_config = tmp_path / "obsai.toml"
    local_config.write_text('[vault]\npath = "/tmp/obsai-toml"\n')
    dot_obsai = tmp_path / ".obsai"
    dot_obsai.mkdir()
    (dot_obsai / "config.toml").write_text('[vault]\npath = "/tmp/dot-obsai"\n')
    path, exists = discover_config_path(tmp_path)
    assert exists is True
    assert path == local_config
    assert load_settings().vault.path == Path("/tmp/obsai-toml")


def test_discover_config_from_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.toml"
    custom.write_text('[vault]\npath = "/tmp/custom-vault"\n')
    monkeypatch.setenv("OBSAI_CONFIG_PATH", str(custom))
    path, exists = discover_config_path()
    assert exists is True
    assert path == custom
    assert load_settings().vault.path == Path("/tmp/custom-vault")


def test_discover_config_env_var_missing_raises_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nonexistent.toml"
    monkeypatch.setenv("OBSAI_CONFIG_PATH", str(missing))
    with pytest.raises(ConfigError, match="specified by OBSAI_CONFIG_PATH does not exist"):
        discover_config_path()


def test_discover_config_fallback_to_global_xdg(tmp_path: Path) -> None:
    xdg_file = tmp_path / "config" / "obsai" / "config.toml"
    xdg_file.parent.mkdir(parents=True)
    xdg_file.write_text('[vault]\npath = "/tmp/xdg-vault"\n')
    path, exists = discover_config_path()
    assert exists is True
    assert path == xdg_file
    assert load_settings().vault.path == Path("/tmp/xdg-vault")


def test_discover_config_zero_config_fallback(tmp_path: Path) -> None:
    path, exists = discover_config_path()
    assert exists is False
    assert path == default_config_path()
    assert load_settings().vault.path is None

