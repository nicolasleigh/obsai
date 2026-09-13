"""Generation-isolated sqlite-vec storage and cross-note embedding cache."""

import json
import math
import re
import sqlite3
from datetime import datetime, timezone

import sqlite_vec

from obsai.embedding.models import EmbeddingGeneration
from obsai.errors import EmbeddingError
from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.retrieval.filters import metadata_conditions
from obsai.storage.database import Database

_HEX = re.compile(r"^[0-9a-f]{64}$")


def _table(generation_id: str) -> str:
    if not _HEX.fullmatch(generation_id):
        raise EmbeddingError("Invalid embedding generation ID")
    return "vec_" + generation_id


def _validate_vector(vector: list[float], dimensions: int) -> None:
    if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("Embedding dimension mismatch or non-finite value")
    if not any(value != 0 for value in vector):
        raise EmbeddingError("Zero embedding cannot be used for cosine search")


def delete_vectors_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    rows = connection.execute(
        """SELECT ce.generation_id, ce.vec_rowid
           FROM chunk_embeddings AS ce JOIN chunks AS c ON c.id = ce.chunk_id
           WHERE c.note_id = ?""",
        (note_id,),
    ).fetchall()
    for row in rows:
        connection.execute(
            f"DELETE FROM {_table(row['generation_id'])} WHERE rowid = ?",
            (row["vec_rowid"],),
        )
    connection.execute(
        "DELETE FROM chunk_embeddings WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE note_id = ?)",
        (note_id,),
    )


class SQLiteVectorStore:
    def __init__(self, database: Database):
        self.db = database

    def has_generation(self, generation: EmbeddingGeneration) -> bool:
        return self.db.connection.execute(
            "SELECT 1 FROM embedding_generations WHERE id = ?", (generation.id,)
        ).fetchone() is not None

    def ensure_generation(self, generation: EmbeddingGeneration) -> None:
        if self.has_generation(generation):
            return
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO embedding_generations
                   (id, provider, model, model_version, dimensions, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (generation.id, generation.provider, generation.model,
                 generation.model_version, generation.dimensions,
                 datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                f"CREATE VIRTUAL TABLE {_table(generation.id)} USING vec0("
                f"embedding float[{generation.dimensions}] distance_metric=cosine)"
            )

    def has_chunk(self, generation: EmbeddingGeneration, chunk_id: str, text_hash: str) -> bool:
        return self.db.connection.execute(
            """SELECT 1 FROM chunk_embeddings WHERE generation_id = ?
               AND chunk_id = ? AND embedding_text_hash = ?""",
            (generation.id, chunk_id, text_hash),
        ).fetchone() is not None

    def get_cached(self, generation: EmbeddingGeneration, text_hash: str) -> list[float] | None:
        row = self.db.connection.execute(
            "SELECT vector FROM embedding_cache WHERE cache_key = ?",
            (generation.cache_key(text_hash),),
        ).fetchone()
        if row is None:
            return None
        import struct
        return list(struct.unpack(f"<{generation.dimensions}f", row["vector"]))

    def has_cache(self, generation: EmbeddingGeneration, text_hash: str) -> bool:
        return self.db.connection.execute(
            "SELECT 1 FROM embedding_cache WHERE cache_key = ?",
            (generation.cache_key(text_hash),),
        ).fetchone() is not None

    def upsert(
        self,
        generation: EmbeddingGeneration,
        chunk_id: str,
        embedding_text_hash: str,
        vector: list[float],
        token_count: int,
    ) -> None:
        _validate_vector(vector, generation.dimensions)
        if token_count < 0:
            raise EmbeddingError("Token count cannot be negative")
        with self.db.transaction() as connection:
            self.ensure_generation(generation)
            chunk = connection.execute(
                "SELECT rowid, embedding_text_hash FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if chunk is None or chunk["embedding_text_hash"] != embedding_text_hash:
                raise EmbeddingError("Chunk is missing or changed since embedding preflight")
            key = generation.cache_key(embedding_text_hash)
            existing_vector = self.get_cached(generation, embedding_text_hash)
            if (existing_vector is not None
                    and any(abs(a - b) > 1e-5 for a, b in zip(existing_vector, vector, strict=True))):
                raise EmbeddingError("Embedding cache key already has a different vector")
            connection.execute(
                """INSERT OR IGNORE INTO embedding_cache
                   (cache_key, generation_id, embedding_text_hash, vector, token_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, generation.id, embedding_text_hash,
                 sqlite_vec.serialize_float32(vector), token_count,
                 datetime.now(timezone.utc).isoformat()),
            )
            previous = connection.execute(
                """SELECT vec_rowid FROM chunk_embeddings
                   WHERE generation_id = ? AND chunk_id = ?""",
                (generation.id, chunk_id),
            ).fetchone()
            if previous is not None:
                connection.execute(
                    f"DELETE FROM {_table(generation.id)} WHERE rowid = ?",
                    (previous["vec_rowid"],),
                )
            connection.execute(
                f"INSERT INTO {_table(generation.id)} (rowid, embedding) VALUES (?, ?)",
                (chunk["rowid"], sqlite_vec.serialize_float32(vector)),
            )
            connection.execute(
                """INSERT INTO chunk_embeddings
                   (generation_id, chunk_id, embedding_text_hash, cache_key, vec_rowid)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(generation_id, chunk_id) DO UPDATE SET
                   embedding_text_hash = excluded.embedding_text_hash,
                   cache_key = excluded.cache_key, vec_rowid = excluded.vec_rowid""",
                (generation.id, chunk_id, embedding_text_hash, key, chunk["rowid"]),
            )

    def delete(self, generation: EmbeddingGeneration, chunk_id: str) -> None:
        if not self.has_generation(generation):
            return
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT vec_rowid FROM chunk_embeddings WHERE generation_id = ? AND chunk_id = ?",
                (generation.id, chunk_id),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                f"DELETE FROM {_table(generation.id)} WHERE rowid = ?", (row["vec_rowid"],)
            )
            connection.execute(
                "DELETE FROM chunk_embeddings WHERE generation_id = ? AND chunk_id = ?",
                (generation.id, chunk_id),
            )

    def clear_vectors(self) -> None:
        with self.db.transaction() as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM embedding_generations")]
            for generation_id in ids:
                connection.execute(f"DROP TABLE {_table(generation_id)}")
            connection.execute("DELETE FROM chunk_embeddings")
            connection.execute("DELETE FROM embedding_cache")
            connection.execute("DELETE FROM embedding_generations")

    def search(
        self,
        generation: EmbeddingGeneration,
        vector: list[float],
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        if limit <= 0 or not self.has_generation(generation):
            return []
        _validate_vector(vector, generation.dimensions)
        table = _table(generation.id)
        # Filtering after KNN needs all candidates to preserve correct top-k.
        filtered = filters.active if filters is not None else False
        metadata_where, metadata_params = metadata_conditions(filters, notes_alias="n")
        k = (
            self.db.connection.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE generation_id = ?",
                (generation.id,),
            ).fetchone()[0]
            if filtered else limit
        )
        if not k:
            return []
        candidates = self.db.connection.execute(
            f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ?",
            (sqlite_vec.serialize_float32(vector), k),
        ).fetchall()
        results: list[SearchResult] = []
        for candidate in candidates:
            row = self.db.connection.execute(
                """SELECT c.id AS chunk_id, c.heading_path, c.raw_content,
                          n.id AS note_id, n.path, n.title
                   FROM chunks AS c JOIN notes AS n ON n.id = c.note_id
                   JOIN chunk_embeddings AS ce ON ce.chunk_id = c.id
                   WHERE ce.generation_id = ? AND ce.vec_rowid = ?"""
                + (" AND " + " AND ".join(metadata_where) if metadata_where else ""),
                (generation.id, candidate["rowid"], *metadata_params),
            ).fetchone()
            if row is None:
                continue
            results.append(SearchResult(
                chunk_id=row["chunk_id"], note_id=row["note_id"], path=row["path"],
                title=row["title"], heading_path=json.loads(row["heading_path"]),
                snippet=row["raw_content"][:240], score=1.0 - candidate["distance"],
                source="semantic",
            ))
            if len(results) == limit:
                break
        return results
