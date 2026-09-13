"""Repository API for the rebuildable SQLite metadata index."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from obsai.chunking.models import Chunk
from obsai.storage.database import Database
from obsai.vault.models import Block, ParsedNote


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class NoteRecord:
    id: str
    path: str
    title: str
    created_at: str
    modified_at: str
    indexed_at: str
    content_hash: str
    frontmatter: dict
    dataview_fields: dict[str, str]


@dataclass(frozen=True)
class LinkRecord:
    source_note_id: str
    source_block_id: str | None
    target_path: str | None
    target_note_id: str | None
    target_heading: str | None
    target_block_id: str | None
    display_text: str | None
    is_embed: bool
    position: int


@dataclass(frozen=True)
class DirtyNote:
    path: str
    note_id: str | None
    reason: str
    marked_at: str


def _note_record(row: sqlite3.Row | None) -> NoteRecord | None:
    if row is None:
        return None
    return NoteRecord(
        id=row["id"],
        path=row["path"],
        title=row["title"],
        created_at=row["created_at"],
        modified_at=row["modified_at"],
        indexed_at=row["indexed_at"],
        content_hash=row["content_hash"],
        frontmatter=json.loads(row["frontmatter_json"]),
        dataview_fields=json.loads(row["dataview_json"]),
    )


class NoteRepository:
    def __init__(self, database: Database):
        self.db = database

    def get(self, note_id: str) -> NoteRecord | None:
        row = self.db.connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _note_record(row)

    def get_by_path(self, path: str) -> NoteRecord | None:
        row = self.db.connection.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()
        return _note_record(row)

    def get_parsed(self, note_id: str) -> ParsedNote | None:
        row = self.db.connection.execute(
            "SELECT parsed_json FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        return ParsedNote.model_validate_json(row[0]) if row is not None else None

    def update_path(self, note_id: str, new_path: str) -> None:
        """Rename a derived record without changing its note or chunk IDs."""
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT parsed_json, path FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            parsed = json.loads(row["parsed_json"])
            parsed["path"] = new_path
            connection.execute(
                "UPDATE notes SET path = ?, parsed_json = ?, indexed_at = ? WHERE id = ?",
                (new_path, _json(parsed), _now(), note_id),
            )
            for chunk in connection.execute(
                "SELECT id, metadata_json FROM chunks WHERE note_id = ?", (note_id,)
            ).fetchall():
                metadata = json.loads(chunk["metadata_json"])
                metadata["path"] = new_path
                connection.execute(
                    "UPDATE chunks SET metadata_json = ? WHERE id = ?",
                    (_json(metadata), chunk["id"]),
                )
            connection.execute(
                "UPDATE dirty_notes SET path = ? WHERE note_id = ?", (new_path, note_id)
            )

    def delete(self, note_id: str) -> None:
        """Delete the note and all source-derived child rows via FK cascades."""
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))


class ChunkRepository:
    def __init__(self, database: Database):
        self.db = database

    def replace_for_note(self, note_id: str, chunks: list[Chunk]) -> None:
        """Replace a note's chunks atomically, binding provisional IDs to the stable note ID."""
        ordered = sorted(chunks, key=lambda chunk: chunk.position)
        if [chunk.position for chunk in ordered] != list(range(len(ordered))):
            raise ValueError("Chunk positions must be contiguous from zero")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            connection.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
            for chunk in ordered:
                if chunk.metadata.get("path") != row["path"]:
                    raise ValueError("Chunk path does not match the indexed note")
                if _hash(chunk.raw_content) != chunk.content_hash:
                    raise ValueError("Chunk content hash does not match its content")
                if _hash(chunk.embedding_text) != chunk.embedding_text_hash:
                    raise ValueError("Chunk embedding hash does not match its text")
                metadata = {**chunk.metadata, "path": row["path"]}
                chunk_id = _hash(f"{note_id}:{chunk.position}:{chunk.content_hash}")
                connection.execute(
                    """INSERT INTO chunks (
                        id, note_id, heading_path, block_id, raw_content, embedding_text,
                        content_hash, embedding_text_hash, token_count, position, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        note_id,
                        _json(chunk.heading_path),
                        chunk.block_id,
                        chunk.raw_content,
                        chunk.embedding_text,
                        chunk.content_hash,
                        chunk.embedding_text_hash,
                        chunk.token_count,
                        chunk.position,
                        _json(metadata),
                    ),
                )

    def list_for_note(self, note_id: str) -> list[Chunk]:
        rows = self.db.connection.execute(
            "SELECT * FROM chunks WHERE note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            Chunk(
                chunk_id=row["id"],
                note_id=row["note_id"],
                heading_path=json.loads(row["heading_path"]),
                block_id=row["block_id"],
                raw_content=row["raw_content"],
                embedding_text=row["embedding_text"],
                content_hash=row["content_hash"],
                embedding_text_hash=row["embedding_text_hash"],
                token_count=row["token_count"],
                position=row["position"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]


class IndexRepository:
    def __init__(self, database: Database):
        self.db = database
        self.notes = NoteRepository(database)
        self.chunks = ChunkRepository(database)

    def index_note(
        self, note: ParsedNote, chunks: list[Chunk], *, note_id: str | None = None
    ) -> str:
        """Store a parsed note and all derived rows as one transaction."""
        with self.db.transaction() as connection:
            existing = self.notes.get(note_id) if note_id is not None else self.notes.get_by_path(note.path)
            if note_id is not None and existing is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            stable_id = existing.id if existing else uuid4().hex
            if existing is not None and existing.path != note.path:
                self.notes.update_path(stable_id, note.path)
            now = _now()
            content_hash = _hash(note.raw_content)
            parsed_data = note.model_dump(mode="json")
            if existing is None:
                connection.execute(
                    """INSERT INTO notes (
                        id, path, title, created_at, modified_at, indexed_at,
                        content_hash, frontmatter_json, dataview_json, parsed_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stable_id,
                        note.path,
                        note.title,
                        now,
                        now,
                        now,
                        content_hash,
                        _json(parsed_data["frontmatter"]),
                        _json(note.dataview_fields),
                        _json(parsed_data),
                    ),
                )
            else:
                modified_at = now if existing.content_hash != content_hash else existing.modified_at
                connection.execute(
                    """UPDATE notes SET title = ?, modified_at = ?, indexed_at = ?,
                        content_hash = ?, frontmatter_json = ?, dataview_json = ?, parsed_json = ?
                        WHERE id = ?""",
                    (
                        note.title,
                        modified_at,
                        now,
                        content_hash,
                        _json(parsed_data["frontmatter"]),
                        _json(note.dataview_fields),
                        _json(parsed_data),
                        stable_id,
                    ),
                )

            self.chunks.replace_for_note(stable_id, chunks)
            connection.execute("DELETE FROM tags WHERE note_id = ?", (stable_id,))
            connection.executemany(
                "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
                [(stable_id, tag) for tag in dict.fromkeys(note.tags)],
            )
            connection.execute("DELETE FROM blocks WHERE note_id = ?", (stable_id,))
            connection.executemany(
                """INSERT INTO blocks (
                    note_id, position, kind, content, raw_content, line, end_line, block_id, language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        stable_id, position, block.kind, block.content, block.raw_content,
                        block.line, block.end_line, block.block_id, block.language,
                    )
                    for position, block in enumerate(note.blocks)
                ],
            )
            connection.execute("DELETE FROM links WHERE source_note_id = ?", (stable_id,))
            for position, link in enumerate(note.wikilinks):
                source_block_id = next(
                    (
                        block.block_id
                        for block in note.blocks
                        if block.block_id and block.line <= link.line <= block.end_line
                    ),
                    None,
                )
                connection.execute(
                    """INSERT INTO links (
                        source_note_id, source_block_id, target_path, target_note_id,
                        target_heading, target_block_id, display_text, is_embed, position
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stable_id,
                        source_block_id,
                        link.target_path,
                        self._target_id(stable_id, note.path, link.target_path),
                        link.target_heading,
                        link.target_block_id,
                        link.display_text,
                        int(link.is_embed),
                        position,
                    ),
                )
            connection.execute("DELETE FROM dirty_notes WHERE path = ?", (note.path,))
            self.reconcile_links()
            return stable_id

    def _target_id(self, source_id: str, source_path: str, target: str | None) -> str | None:
        if target is None:
            return source_id
        path = PurePosixPath(target)
        candidates = [target]
        if path.suffix.lower() != ".md":
            candidates.append(f"{target}.md")
        parent = PurePosixPath(source_path).parent
        if str(parent) != ".":
            candidates.extend(str(parent / candidate) for candidate in tuple(candidates))
        for candidate in candidates:
            record = self.notes.get_by_path(candidate)
            if record is not None:
                return record.id
        return None

    def reconcile_links(self) -> None:
        """Resolve links whose target was indexed after their source."""
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT links.id, links.source_note_id, links.target_path, notes.path AS source_path
                   FROM links JOIN notes ON notes.id = links.source_note_id
                   WHERE links.target_note_id IS NULL"""
            ).fetchall()
            for row in rows:
                target_id = self._target_id(
                    row["source_note_id"], row["source_path"], row["target_path"]
                )
                if target_id is not None:
                    connection.execute(
                        "UPDATE links SET target_note_id = ? WHERE id = ?",
                        (target_id, row["id"]),
                    )

    def tags_for_note(self, note_id: str) -> list[str]:
        return [
            row[0] for row in self.db.connection.execute(
                "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,)
            )
        ]

    def links_for_note(self, note_id: str) -> list[LinkRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM links WHERE source_note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            LinkRecord(
                source_note_id=row["source_note_id"],
                source_block_id=row["source_block_id"],
                target_path=row["target_path"],
                target_note_id=row["target_note_id"],
                target_heading=row["target_heading"],
                target_block_id=row["target_block_id"],
                display_text=row["display_text"],
                is_embed=bool(row["is_embed"]),
                position=row["position"],
            )
            for row in rows
        ]

    def blocks_for_note(self, note_id: str) -> list[Block]:
        rows = self.db.connection.execute(
            "SELECT * FROM blocks WHERE note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            Block(
                kind=row["kind"], content=row["content"], raw_content=row["raw_content"],
                line=row["line"], end_line=row["end_line"], block_id=row["block_id"],
                language=row["language"],
            )
            for row in rows
        ]

    def get_state(self, key: str) -> str | None:
        row = self.db.connection.execute(
            "SELECT value FROM index_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO index_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = excluded.updated_at""",
                (key, value, _now()),
            )

    def mark_dirty(self, path: str, reason: str) -> None:
        with self.db.transaction() as connection:
            record = self.notes.get_by_path(path)
            connection.execute(
                """INSERT INTO dirty_notes (path, note_id, reason, marked_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET
                   note_id = excluded.note_id, reason = excluded.reason,
                   marked_at = excluded.marked_at""",
                (path, record.id if record else None, reason, _now()),
            )

    def list_dirty(self) -> list[DirtyNote]:
        rows = self.db.connection.execute(
            "SELECT * FROM dirty_notes ORDER BY path"
        ).fetchall()
        return [DirtyNote(**dict(row)) for row in rows]

    def clear(self) -> None:
        """Drop all derived data while retaining the initialized schema."""
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM dirty_notes")
            connection.execute("DELETE FROM notes")
            connection.execute("DELETE FROM index_state")
