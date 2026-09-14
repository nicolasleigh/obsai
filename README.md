# ObsAgent CLI

Local-first Obsidian CLI with Markdown parsing, context-aware chunking, a
rebuildable SQLite index, hybrid retrieval, and evidence-bounded answers.
`obsai index update` synchronizes a configured Vault.

```bash
uv sync
uv run obsai --help
uv run obsai --version
uv run obsai status
uv run obsai index update
uv run obsai search "context.WithTimeout" --mode keyword
uv run obsai index embeddings
uv run obsai search "服务怎么平滑退出？" --mode semantic
uv run obsai ask "我以前如何理解 graceful shutdown？"
uv run obsai note --help
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
`note_id=note_id` to `index_note`. The schema uses `PRAGMA user_version = 3` and enables foreign keys on
every connection. Version 1 and 2 databases migrate automatically; version 1
databases also backfill FTS5 rows.
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
uv run obsai search "context.WithTimeout" --mode keyword --tag go --folder Backend --json
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

## Semantic retrieval

First run `obsai index update`, then `obsai index embeddings`. The embedding
command prints the number of unique texts requiring remote generation, cache
reuse, conservative token upper bound, request count, and estimated cost. It
requires an interactive confirmation before sending text to OpenAI. Set
`OPENAI_API_KEY` in the environment for approved calls. Tests use a mock provider
and never send real notes or queries to OpenAI. Semantic search also asks before
sending its query for embedding. There is no automatic local-provider fallback.

The optional config fields are:

```toml
[embedding]
provider = "openai"
model = "text-embedding-3-small"
model_version = "text-embedding-3-small"
dimensions = 1536
batch_size = 64
max_concurrency = 2
max_input_tokens = 8192
max_request_tokens = 300000
timeout_seconds = 30
max_attempts = 4
max_embedding_tokens = 1000000
estimated_cost_limit_usd = 1.0
max_embedding_requests = 1000
# price_per_million_tokens_usd = 0.02
```

The default price estimate for `text-embedding-3-small` is $0.02 per million
input tokens, based on [official OpenAI model pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small).
Override it when provider pricing changes. The token estimate uses UTF-8 byte
length as a conservative upper bound: it can overestimate both provider tokens
and cost, but cannot silently undercount per-input or per-request limits.
Provider, model, model version, dimensions, and text hash define the cache key.
Each generation has a separate sqlite-vec `vec0` table. Updating
`model_version` is necessary if an upstream model alias changes its embedding
space. Pure Vault renames retain the existing vector mapping; changed chunks
can reuse previously cached embeddings when their embedding text hash matches.

## Hybrid retrieval

`obsai search "..."` defaults to hybrid retrieval. It requests candidate
chunks from FTS5 and the current vector generation, then merges ranks with
Reciprocal Rank Fusion (`k=60`) and returns the top results. `--mode keyword`
and `--mode semantic` select one path. Hybrid JSON results include `sources`
to show whether a chunk appeared in keyword search, semantic search, or both.
`NoOpReranker` keeps the fused order; the `Reranker` interface accepts a later
reranking implementation without changing retrieval backends.

The shared metadata filters are `--folder`, repeatable `--tag`,
`--modified-after`, `--modified-before`, repeatable `--frontmatter key=value`,
and repeatable `--dataview key=value`. Frontmatter filtering supports top-level
scalar values. `modified` compares the database's content-index timestamp,
not a filesystem mtime. Dataview fields are compared as stored strings; no
Dataview query is executed.

Hybrid search visibly falls back to keyword results when there is no vector
generation, the user declines remote query embedding, or the semantic backend
fails. The warning goes to stderr and is also available through
`HybridRetriever.search_with_status().warnings`. Use `--strict-semantic` to
fail instead of degrading. Semantic-only mode always fails when its backend
is unavailable. Keyword mode never calls the embedding provider.

The small benchmark dataset at `tests/fixtures/retrieval/benchmark.json`
contains queries and expected note paths. `obsai.retrieval.evaluation.evaluate`
calculates macro Recall@K, MRR, and Precision@K. The mock-backed integration
benchmark checks that hybrid scores do not fall below either single path at
K=2; it does not measure real OpenAI embedding quality.

## Ask with citations

`obsai ask "..."` performs one hybrid retrieval and one LLM request. It loads
the selected chunks' original content from SQLite; FTS snippets are never used
as evidence. The ContextBuilder keeps ranked results, removes repeated chunk
IDs, and enforces all three independent limits below. Evidence sent to the
provider carries `[S1]`, `[S2]`, etc., with path, title, heading, and block
metadata. The CLI prints only sources cited in the accepted answer.

```toml
[ask]
provider = "openai"
model = "gpt-4.1-mini"
timeout_seconds = 60
max_output_tokens = 1024
max_context_tokens = 12000
max_evidence_tokens = 2500
max_chunks = 6
```

Set `OPENAI_API_KEY` for a real answer. If a vector generation is available,
the CLI requests confirmation before embedding the query, as with hybrid
search. A missing or failed semantic backend is reported and keyword results
are used. With no evidence, the CLI abstains without calling the LLM. If the
model returns an unknown citation ID or no citation, its answer is discarded
and an abstention is shown. Limits use UTF-8 byte length as a conservative,
offline token upper bound and may admit less evidence than a model tokenizer.
Long evidence is visibly truncated. Citation checking verifies source IDs,
not whether every natural-language claim faithfully paraphrases its source;
the prompt requires grounded, cited answers. Tests replace the LLM adapter and
make no remote calls. The Vault remains read-only.

## Safe single-note writes

All CLI note mutations go through `SafeWriteService`, directly or through
`TransactionService`. `obsai note create`,
`update`, `move`, `trash`, and `frontmatter` first print a Rich-colored unified
diff and ask for yes/no approval; no is the default. `update` replaces one
exact text span, and `frontmatter` changes the YAML header while preserving
the Markdown body. To review an edit:

```bash
uv run obsai note create "Go/new.md" --content "# New note"
uv run obsai note update "Go/context.md" --old "timeout: 5s" --new "timeout: 10s"
uv run obsai note frontmatter "Go/context.md" --set status=done
uv run obsai note move "Go/context.md" "Archive/context.md"
uv run obsai note trash "Archive/context.md"
```

The service hashes the source at preparation and checks it again immediately
before commit. Changed or disappeared sources raise `ConflictError`, and
existing destinations raise `CollisionError`. Relative Markdown paths are
confined to the configured Vault; symlinked components and traversal are
rejected. Content updates use a flushed and fsynced temporary file in the
same directory before replacement. Trash moves notes under the hidden
`.obsai-trash/` directory instead of deleting them. Run `obsai index update`
after a single-note create, update, trash, or frontmatter change to refresh
the derived SQLite index.

## Multi-file transactions and recovery

`TransactionService.plan([...])` simulates a batch of create, exact replacement,
frontmatter, move, and trash operations without writing. `preflight` checks
every original hash, path, destination, required permission, device, and free
space before snapshots or edits begin. After approval, the service writes
short-lived byte snapshots and a durable journal under `.obsai-transactions/`,
applies each change through `SafeWriteService`, verifies the final files, and
rolls back applied changes if a file operation fails.

```python
from obsai.transactions import TransactionOperation as Op, TransactionService

service = TransactionService(vault_path, database_path=index_path)
plan = service.plan([
    Op.move("Go/context.md", "Archive/context.md"),
    Op.frontmatter("Archive/context.md", {"status": "archived"}),
    Op.replace("Go/guide.md", "old wording", "new wording"),
])
service.preview(plan, console)
result = service.execute(plan, approved=True)
```

`obsai note move` uses this transaction path. It rewrites only parser-confirmed
WikiLinks with an explicit vault-root path, such as `[[Go/context#Heading|Alias]]`.
Basename-only links such as `[[context]]`, mixed code/text lines, and other
uncertain targets stay unchanged and are reported. A successful Vault commit
followed by a failed index update is **not** rolled back: the journal records
`index_dirty`, and the SQLite repository marks affected paths dirty when it is
available. `obsai index update` reconciles and clears that state.

An interrupted run leaves a journal. The CLI warns on the next invocation;
new writes and reindexing are blocked until recovery. Use
`obsai transaction status` to inspect affected paths and
`obsai transaction recover ID` to preview a diff and confirm rollback from snapshots. Recovery
refuses to overwrite files that no longer match a known transaction state.

## Bounded agent workflow

`obsai agent run "搜索 context"` performs a direct search without a planning loop.
Questions use the existing hybrid retrieval, bounded `ContextBuilder`, LLM answer,
and citation validation. Read, write, and organization requests enter a LangGraph
workflow that selects from `search_notes`, `read_note`, `get_backlinks`,
`get_outgoing_links`, `create_note`, `update_note`, `move_note`, `trash_note`, and
`update_frontmatter`. The OpenAI planner requires `OPENAI_API_KEY`; tests use a
mock planner and make no remote calls.

The workflow stops after 15 tool steps, 5 retrievals, 3 consecutive errors, 2
identical tool calls, or 3 steps without progress. It reports why it stopped.
Checkpoint state stores note IDs, chunk IDs, and artifact references rather than
note bodies or transaction snapshots. Workflow checkpoints and tool artifacts
are stored beside the configured index as `agent-checkpoints.db` and
`agent-artifacts.db`. They are distinct from the Vault transaction journal.

Every write tool first creates a transaction plan and displays a diff. It then
interrupts before applying anything. Resume with `obsai agent resume WORKFLOW_ID`
to review the diff again and answer the yes/no prompt. A rejected plan leaves
the Vault unchanged. Approval executes through `TransactionService`, which
rechecks source hashes and handles rollback and index failure as described above.
The CLI prints the workflow ID with each pending approval; pass `--thread-id`
to `agent run` if you need a predetermined ID. Checkpoint resume survives a
process restart as long as both agent SQLite files remain available.
