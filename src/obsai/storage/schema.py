"""Versioned SQLite schema. Schema 0 is a new, empty database."""

import sqlite3

from obsai.errors import SchemaError

SCHEMA_VERSION = 1
REQUIRED_TABLES = {
    "notes", "chunks", "tags", "links", "blocks", "index_state", "dirty_notes"
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


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if missing := REQUIRED_TABLES - tables:
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        return
    if version != 0:
        raise SchemaError(f"Unsupported SQLite schema version: {version}")

    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    if existing is not None:
        raise SchemaError("Unversioned SQLite database is not empty")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_V1:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
