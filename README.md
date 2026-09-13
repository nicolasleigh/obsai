# ObsAgent CLI

Local-first Obsidian CLI with a read-only Phase 1 Markdown parser. The CLI still
exposes only the Phase 0 commands; parsing is available through the Python API.

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

## Read-only vault parsing

```python
from pathlib import Path
from obsai.vault import parse_vault, scan_markdown_files

vault = Path("/path/to/vault")
paths = scan_markdown_files(vault)
notes = parse_vault(vault)  # list[ParsedNote], sorted by vault-relative path
```

The parser reads UTF-8 Markdown and optional YAML frontmatter. It extracts
headings, paragraphs, lists, code blocks, WikiLinks and embeds, tags, external
links, callouts, block references, and Dataview `key:: value` fields. It never
renders HTML or executes code, Dataview queries, or JavaScript. The vault-root
`.obsaiignore` uses gitignore-style patterns; hidden and symlinked files are
also skipped. Ignored directories are pruned before reading, so negation rules
cannot re-include files inside an excluded directory in this phase.
