# ObsAgent CLI

Local-first Obsidian CLI with read-only Markdown parsing and context-aware
chunking. The CLI still exposes only the Phase 0 commands; parsing and chunking
are available through the Python API.

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

## Context-aware chunking

```python
from obsai.chunking import ChunkingOptions, chunk_note

chunks = chunk_note(notes[0], ChunkingOptions(min_tokens=80, target_tokens=260, max_tokens=400))
```

Chunks follow note heading hierarchy and split long sections at paragraph
boundaries. A fenced code block, callout, or paragraph containing a block ID
remains intact even if it exceeds `max_tokens`; such a chunk has
`metadata["oversized_atomic"] = True`. `raw_content` keeps the Markdown body
without injected labels. `embedding_text` adds the title and section breadcrumb,
but not the filesystem path. The first H1 is omitted from its `Section` label
when it equals the note title; `heading_path` always retains the full hierarchy.
`token_count` is a deterministic, model-independent estimate of embedding text
size. Phase 3 will need a persistent note ID; Phase 2 derives `note_id` from the
vault-relative path.
