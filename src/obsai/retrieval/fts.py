"""SQLite FTS5 keyword retrieval over indexed chunks."""

import json

from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.storage import Database
from obsai.storage.fts import cjk_text


def _phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class FTSRetriever:
    def __init__(self, database: Database):
        self.db = database

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        query = query.strip()
        if not query or not any(char.isalnum() for char in query) or limit <= 0:
            return []

        # Treat user input as a literal phrase, never as FTS query syntax.
        phrase = _phrase(query)
        if cjk_text(query) != query:
            match = f"({phrase} OR cjk_text:{_phrase(cjk_text(query))})"
        else:
            match = phrase

        where = ["chunk_fts MATCH ?"]
        params: list[object] = [match]
        if filters is not None:
            for tag in filters.tags:
                where.append(
                    "EXISTS (SELECT 1 FROM tags WHERE tags.note_id = notes.id AND tags.tag = ?)"
                )
                params.append(tag.lstrip("#"))
            if filters.folder is not None:
                folder = filters.folder.strip("/")
                if folder:
                    prefix = folder + "/"
                    where.append("substr(notes.path, 1, length(?)) = ?")
                    params.extend((prefix, prefix))

        rows = self.db.connection.execute(
            """SELECT chunks.id AS chunk_id, notes.id AS note_id, notes.path, notes.title,
                      chunks.heading_path,
                      snippet(chunk_fts, 2, '[', ']', '…', 24) AS snippet,
                      bm25(chunk_fts, 5.0, 3.0, 1.0, 2.0, 0.8) AS rank
               FROM chunk_fts
               JOIN chunks ON chunks.rowid = chunk_fts.rowid
               JOIN notes ON notes.id = chunks.note_id
               WHERE """
            + " AND ".join(where)
            + " ORDER BY rank, notes.path, chunks.position LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [
            SearchResult(
                chunk_id=row["chunk_id"],
                note_id=row["note_id"],
                path=row["path"],
                title=row["title"],
                heading_path=json.loads(row["heading_path"]),
                snippet=row["snippet"],
                score=-row["rank"],
                source="keyword",
            )
            for row in rows
        ]
