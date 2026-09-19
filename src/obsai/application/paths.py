"""Resolving Vault and index locations from configuration.

``cli/app.py`` used to repeat the same two derivations and the same two error
strings at a dozen call sites. Centralising them here keeps the messages
byte-identical across every command while giving the HTTP adapter the same
behaviour for free.
"""

from __future__ import annotations

from pathlib import Path

from obsai.config.models import Settings
from obsai.errors import ConfigError

NO_VAULT_MESSAGE = "No vault configured; set vault.path in config.toml"
MISSING_INDEX_MESSAGE = "Index does not exist; run 'obsai index update' first"
MISSING_VAULT_DIRECTORY_MESSAGE = "Vault directory does not exist: {path}"


def database_path(settings: Settings) -> Path:
    """The index location, falling back to the historical ``~/.obsai`` default."""
    return (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()


def require_vault(settings: Settings) -> Path:
    """Return the configured Vault root, or fail with the standard message."""
    if settings.vault.path is None:
        raise ConfigError(NO_VAULT_MESSAGE)
    return settings.vault.path.expanduser()


def require_index(settings: Settings) -> Path:
    """Return the index path, or fail when it has not been built yet."""
    path = database_path(settings)
    if not path.is_file():
        raise ConfigError(MISSING_INDEX_MESSAGE)
    return path


def require_existing_vault(settings: Settings) -> Path:
    """The configured Vault root, verified to be a directory.

    :func:`require_vault` answers "is one configured"; this answers "is it
    there". The two are separate because they are answerable at different times:
    a background job that discovers a missing Vault has already returned a 202,
    so the caller learns about it from a failed job instead of from the request
    that started it. Resolving this before the job is built is what keeps the
    answer in the request.

    A symlinked Vault root is accepted — the scanner resolves it — so this is
    ``is_dir`` rather than "a directory that is not a link".
    """
    vault = require_vault(settings)
    if not vault.is_dir():
        raise ConfigError(MISSING_VAULT_DIRECTORY_MESSAGE.format(path=vault))
    return vault


def require_vault_and_index(settings: Settings) -> tuple[Path, Path]:
    """Both, in the order callers usually need them."""
    return require_vault(settings), require_index(settings)
