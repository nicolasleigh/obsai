"""Read canonical evidence from the derived index, never from search snippets."""

import json

from obsai.answering.models import EvidenceRecord
from obsai.storage.database import Database


class SQLiteEvidenceRepository:
    def __init__(self, database: Database):
        self.db = database

    def get(self, chunk_id: str) -> EvidenceRecord | None:
        row = self.db.connection.execute(
            """SELECT chunks.id AS chunk_id, chunks.note_id, notes.path, notes.title,
                      chunks.heading_path, chunks.block_id, chunks.raw_content
               FROM chunks JOIN notes ON notes.id = chunks.note_id
               WHERE chunks.id = ?""",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return EvidenceRecord(
            chunk_id=row["chunk_id"], note_id=row["note_id"], path=row["path"],
            title=row["title"], heading_path=tuple(json.loads(row["heading_path"])),
            block_id=row["block_id"], raw_content=row["raw_content"],
        )
