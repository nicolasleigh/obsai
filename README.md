# ObsAgent CLI

Phase 0 foundation for a local-first Obsidian CLI. It does not access or modify a vault.

```bash
uv sync
uv run obsai --help
uv run obsai --version
uv run obsai status
uv run pytest
```

The optional config file is `~/.config/obsai/config.toml` (or
`$XDG_CONFIG_HOME/obsai/config.toml` when set):

```toml
[vault]
path = "/path/to/vault"

[index]
database = "/path/to/index.db"
```

Both fields are optional in Phase 0. `OBSAI_VAULT__PATH` and
`OBSAI_INDEX__DATABASE` can provide values when the config file omits them.
The CLI only reads configuration; it does not create the file or directories.
