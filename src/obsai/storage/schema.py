"""Versioned SQLite schema. Schema 0 is a new, empty database."""

import sqlite3

from obsai.errors import SchemaError
from obsai.storage.fts import refresh_fts_for_note

SCHEMA_VERSION = 3
REQUIRED_TABLES = {
    "notes", "chunks", "tags", "links", "blocks", "index_state", "dirty_notes", "chunk_fts",
    "embedding_generations", "embedding_cache", "chunk_embeddings",
}

SCHEMA_V1 = (
    """CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        modified_at TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        frontmatter_json TEXT NOT NULL,
        dataview_json TEXT NOT NULL,
        parsed_json TEXT NOT NULL
    )""",
    """CREATE TABLE chunks (
        id TEXT PRIMARY KEY,
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        heading_path TEXT NOT NULL,
        block_id TEXT,
        raw_content TEXT NOT NULL,
        embedding_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        embedding_text_hash TEXT NOT NULL,
        token_count INTEGER NOT NULL CHECK(token_count >= 0),
        position INTEGER NOT NULL CHECK(position >= 0),
        metadata_json TEXT NOT NULL,
        UNIQUE(note_id, position)
    )""",
    """CREATE TABLE tags (
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY(note_id, tag)
    )""",
    """CREATE TABLE links (
        id INTEGER PRIMARY KEY,
        source_note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        source_block_id TEXT,
        target_path TEXT,
        target_note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
        target_heading TEXT,
        target_block_id TEXT,
        display_text TEXT,
        is_embed INTEGER NOT NULL CHECK(is_embed IN (0, 1)),
        position INTEGER NOT NULL,
        UNIQUE(source_note_id, position)
    )""",
    """CREATE TABLE blocks (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        raw_content TEXT NOT NULL,
        line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        block_id TEXT,
        language TEXT,
        UNIQUE(note_id, position)
    )""",
    """CREATE TABLE index_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE dirty_notes (
        path TEXT PRIMARY KEY,
        note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
        reason TEXT NOT NULL,
        marked_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_chunks_note ON chunks(note_id, position)",
    "CREATE INDEX idx_tags_tag ON tags(tag)",
    "CREATE INDEX idx_links_target ON links(target_note_id)",
    "CREATE INDEX idx_blocks_reference ON blocks(note_id, block_id)",
)

SCHEMA_V2 = (
    "CREATE VIRTUAL TABLE chunk_fts USING fts5("
    "title, heading, raw_content, tags, cjk_text, tokenize='unicode61')"
)

SCHEMA_V3 = (
    """CREATE TABLE embedding_generations (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        model_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions > 0),
        created_at TEXT NOT NULL,
        UNIQUE(provider, model, model_version, dimensions)
    )""",
    """CREATE TABLE embedding_cache (
        cache_key TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
        embedding_text_hash TEXT NOT NULL,
        vector BLOB NOT NULL,
        token_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE chunk_embeddings (
        generation_id TEXT NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
        embedding_text_hash TEXT NOT NULL,
        cache_key TEXT NOT NULL REFERENCES embedding_cache(cache_key),
        vec_rowid INTEGER NOT NULL,
        PRIMARY KEY(generation_id, chunk_id),
        UNIQUE(generation_id, vec_rowid)
    )""",
    "CREATE INDEX idx_cache_generation_hash ON embedding_cache(generation_id, embedding_text_hash)",
    "CREATE INDEX idx_chunk_embeddings_chunk ON chunk_embeddings(chunk_id)",
)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _migrate_v2(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(SCHEMA_V2)
        for (note_id,) in connection.execute("SELECT id FROM notes"):
            refresh_fts_for_note(connection, note_id)
        connection.execute("PRAGMA user_version = 2")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_v3(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_V3:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        if missing := REQUIRED_TABLES - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        return
    if version not in (0, 1, 2):
        raise SchemaError(f"Unsupported SQLite schema version: {version}")

    if version == 2:
        if missing := (REQUIRED_TABLES - {"embedding_generations", "embedding_cache", "chunk_embeddings"}) - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        _migrate_v3(connection)
        return

    if version == 1:
        if missing := (REQUIRED_TABLES - {"chunk_fts", "embedding_generations", "embedding_cache", "chunk_embeddings"}) - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        _migrate_v2(connection)
        _migrate_v3(connection)
        return

    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    if existing is not None:
        raise SchemaError("Unversioned SQLite database is not empty")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    _migrate_v2(connection)
    _migrate_v3(connection)
