"""SQL stays behind a graph DAO; the Vault remains the source of truth."""

from obsai.graph.models import GraphEdge
from obsai.retrieval.filters import metadata_conditions
from obsai.retrieval.models import SearchFilters
from obsai.storage import Database


_EDGE_SELECT = """SELECT l.id, l.source_note_id, source.path AS source_path,
       l.source_block_id, l.target_path, l.target_note_id,
       target.path AS resolved_target_path, l.target_heading, l.target_block_id,
       l.display_text, l.is_embed, l.position
FROM links AS l
JOIN notes AS source ON source.id = l.source_note_id
LEFT JOIN notes AS target ON target.id = l.target_note_id
"""


def _edges(rows) -> list[GraphEdge]:
    return [GraphEdge(**{**dict(row), "is_embed": bool(row["is_embed"])}) for row in rows]


class GraphRepository:
    def __init__(self, database: Database):
        self.database = database

    def outgoing(self, note_id: str, *, limit: int | None = None) -> list[GraphEdge]:
        sql = _EDGE_SELECT + "WHERE l.source_note_id = ? ORDER BY l.position, l.id"
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.database.connection.execute(
            sql, (note_id,) if limit is None else (note_id, limit),
        ).fetchall()
        return _edges(rows)

    def backlinks(self, note_id: str, *, limit: int | None = None) -> list[GraphEdge]:
        sql = _EDGE_SELECT + "WHERE l.target_note_id = ? ORDER BY source.path, l.position, l.id"
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.database.connection.execute(
            sql, (note_id,) if limit is None else (note_id, limit),
        ).fetchall()
        return _edges(rows)

    def note_matches(self, note_id: str, filters: SearchFilters | None) -> bool:
        clauses, params = metadata_conditions(filters)
        sql = "SELECT 1 FROM notes WHERE notes.id = ?"
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        return self.database.connection.execute(sql, (note_id, *params)).fetchone() is not None
