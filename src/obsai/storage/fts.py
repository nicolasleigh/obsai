"""Derived FTS5 rows and Unicode search normalization."""

import json
import re
import sqlite3

_HAN = re.compile(r"([\u3400-\u9fff])")


def cjk_text(value: str) -> str:
    """Expose Han characters as adjacent FTS tokens for substring phrases."""
    return " ".join(_HAN.sub(r" \1 ", value).split())


def delete_fts_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    connection.execute(
        "DELETE FROM chunk_fts WHERE rowid IN "
        "(SELECT rowid FROM chunks WHERE note_id = ?)",
        (note_id,),
    )


def refresh_fts_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    """Refresh title, headings, body, and tags from their source tables."""
    delete_fts_for_note(connection, note_id)
    note = connection.execute("SELECT title FROM notes WHERE id = ?", (note_id,)).fetchone()
    if note is None:
        raise KeyError(f"Unknown note ID: {note_id}")
    tags = " ".join(
        row[0] for row in connection.execute(
            "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,)
        )
    )
    rows = connection.execute(
        "SELECT rowid, heading_path, raw_content FROM chunks WHERE note_id = ?",
        (note_id,),
    ).fetchall()
    for row in rows:
        heading = " > ".join(json.loads(row["heading_path"]))
        fields = (note["title"], heading, row["raw_content"], tags)
        connection.execute(
            "INSERT INTO chunk_fts (rowid, title, heading, raw_content, tags, cjk_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["rowid"], *fields, cjk_text(" ".join(fields))),
        )
