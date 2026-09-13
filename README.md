# ObsAgent CLI

Local-first Obsidian CLI with Markdown parsing, context-aware chunking, and a
rebuildable SQLite metadata index. `obsai index update` synchronizes a configured
Vault; the underlying layers are also available through the Python API.

```bash
uv sync
uv run obsai --help
uv run obsai --version
uv run obsai status
uv run obsai index update
uv run obsai search "context.WithTimeout" --mode keyword
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

Both fields are optional in the configuration model. `OBSAI_VAULT__PATH` and
`OBSAI_INDEX__DATABASE` can provide values when the config file omits them.
The CLI reads but does not create the config file. `index update` requires a
Vault path and creates the SQLite database at `~/.obsai/index.db` if
`index.database` is omitted.

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
size. Chunks produced here have provisional path-derived IDs; the repository
rebinds them to a persistent note ID when indexing.

## SQLite metadata index

```python
from pathlib import Path
from obsai.storage import Database, IndexRepository

with Database(Path("/path/to/index.db")) as db:
    index = IndexRepository(db)
    note_id = index.index_note(notes[0], chunk_note(notes[0]))
    stored_note = index.notes.get_parsed(note_id)
    stored_chunks = index.chunks.list_for_note(note_id)
    index.notes.update_path(note_id, "New/location.md")
```

The first insert assigns a UUID-based note ID. Calling `update_path` with that
ID preserves it and the existing chunk IDs; later reindexing can pass
`note_id=note_id` to `index_note`. The schema uses `PRAGMA user_version = 2` and enables foreign keys on
every connection. Version 1 databases migrate automatically and backfill FTS5 rows.
`IndexRepository.clear()` removes derived rows for a rebuild.
`created_at`, `modified_at`, and `indexed_at` are index timestamps in UTC, not
filesystem birth or modification times. All database writes stay inside the
repository layer; the Vault remains the source of truth.

## Incremental update

`obsai index update` hashes each visible, non-ignored Markdown file. Unchanged
files are not re-parsed or re-chunked. A disappeared indexed path and a new path
with one unique exact content-hash match are reported as a rename or move;
their note ID, chunk IDs, and embedding text hashes are retained. Ambiguous
same-content matches are conservatively treated as deletes and creates.
Changed files are re-parsed and re-chunked, and deleted files are removed from
the index with their derived rows. The update reports affected WikiLinks for
renames and moves but never changes Vault files or backlinks. It does not use
watcher events or `.obsidian/workspace.json` as a source of truth. A note whose
title came only from its old filename retains that indexed title after a pure
rename so its embedding text stays stable; its title is recomputed when the
content is later reindexed.

## Keyword retrieval

```bash
uv run obsai search "graceful shutdown" --mode keyword --limit 10
uv run obsai search "context.WithTimeout" --tag go --folder Backend --json
```

Search uses local SQLite FTS5 over note title, heading breadcrumb, chunk body,
and tags. Vault paths are used for folder filtering and returned as metadata;
they are not indexed as body text. Queries are escaped as literal phrases, so
FTS syntax in a query is never executed. Repeated `--tag` options require all
tags. Han characters are additionally indexed as individual tokens to support
Chinese substring phrases. Results contain chunk and note IDs, path, title,
heading path, snippet, score, and `source="keyword"`. Index writes, updates,
deletes, and rollbacks keep FTS rows in the same transaction. This phase does
not use an LLM or semantic search. The test suite includes a small synthetic
P95 smoke benchmark; the 10k-note/100k-chunk target still needs profiling at
that scale.
