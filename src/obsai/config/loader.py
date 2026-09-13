"""Read optional TOML config without creating files or directories."""

import os
import tomllib
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError

from obsai.config.models import Settings
from obsai.errors import ConfigError


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "obsai" / "config.toml"


def load_settings(path: Path | None = None) -> Settings:
    config_path = path if path is not None else default_config_path()
    try:
        with config_path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError as exc:
        if path is not None:
            raise ConfigError(f"Configuration file not found: {config_path}") from exc
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration {config_path}: {exc}") from exc

    try:
        return Settings(**data)
    except (ValidationError, SettingsError) as exc:
        raise ConfigError(f"Invalid configuration {config_path}: {exc}") from exc
