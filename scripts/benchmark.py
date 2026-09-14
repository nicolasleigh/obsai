"""Reproducible local release benchmark; no network or real Vault access.

Run with `uv run python scripts/benchmark.py`. Defaults to 10k notes / 100k
chunks and vectors. Reduce the sizes for a quick smoke run.
"""

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from obsai.embedding.models import EmbeddingGeneration
from obsai.indexing import IncrementalIndexer
from obsai.retrieval import FTSRetriever, HybridRetriever
from obsai.storage import Database, IndexRepository, SQLiteVectorStore


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def timed_queries(function, queries: list[str]) -> dict[str, float]:
    durations = []
    for query in queries:
        start = time.perf_counter()
        function(query)
        durations.append((time.perf_counter() - start) * 1000)
    return {"p50_ms": round(statistics.median(durations), 2),
            "p95_ms": round(percentile_95(durations), 2)}


class LocalVectorRetriever:
    def __init__(self, store: SQLiteVectorStore, generation: EmbeddingGeneration):
        self.store = store
        self.generation = generation

    def search(self, query: str, limit: int = 10, filters=None):
        # Benchmark the actual sqlite-vec lookup without remote query embedding.
        seed = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16)
        return self.store.search(self.generation, [1.0, (seed % 100) / 100, 0.5], limit, filters)


def run(notes: int, chunks_per_note: int, vector_chunks: int, queries: int) -> dict:
    if min(notes, chunks_per_note, vector_chunks, queries) < 1:
        raise ValueError("All benchmark sizes must be positive")
    with tempfile.TemporaryDirectory(prefix="obsai-benchmark-") as directory:
        root = Path(directory)
        vault = root / "vault"
        vault.mkdir()
        for number in range(notes):
            content = f"# Topic {number}\n\n" + "\n\n".join(
                f"## Section {part}\n\nReliability context {number} section {part}."
                for part in range(chunks_per_note)
            ) + "\n"
            (vault / f"note-{number:05d}.md").write_text(content, encoding="utf-8")

        database_path = root / "index.db"
        start = time.perf_counter()
        with Database(database_path) as db:
            index = IndexRepository(db)
            created = IncrementalIndexer(index).update(vault)
            initial_index_seconds = time.perf_counter() - start
            actual_chunks = db.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            generation = EmbeddingGeneration("benchmark", "deterministic", "v1", 3)
            store = SQLiteVectorStore(db)
            with db.transaction():
                for row in db.connection.execute(
                    "SELECT id, embedding_text_hash FROM chunks LIMIT ?", (vector_chunks,)
                ):
                    seed = int(row["embedding_text_hash"][:8], 16)
                    store.upsert(generation, row["id"], row["embedding_text_hash"],
                                 [1.0, (seed % 100) / 100, 0.5], 20)

            fts = FTSRetriever(db)
            vector = LocalVectorRetriever(store, generation)
            hybrid = HybridRetriever(fts, vector)
            sample = [f"Reliability context {i % notes}" for i in range(queries)]
            # Warm up SQLite caches before recording latency.
            for retriever in (fts, vector, hybrid):
                retriever.search(sample[0])
            fts_result = timed_queries(fts.search, sample)
            vector_result = timed_queries(vector.search, sample)
            hybrid_result = timed_queries(hybrid.search, sample)
            start = time.perf_counter()
            unchanged = IncrementalIndexer(index).update(vault)
            update_seconds = time.perf_counter() - start
            vector_count = db.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]

        executable = Path(sys.executable).parent / "obsai"
        startup = timed_queries(
            lambda _: subprocess.run([str(executable), "--help"], check=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            [""] * queries,
        )
        return {
            "requested_notes": notes, "indexed_notes": created.count("created"),
            "requested_chunks_per_note": chunks_per_note, "indexed_chunks": actual_chunks,
            "indexed_vectors": vector_count, "queries": queries,
            "cli_startup": startup, "fts": fts_result, "vector": vector_result,
            "hybrid": hybrid_result,
            "initial_index_seconds": round(initial_index_seconds, 2),
            "unchanged_index_update_seconds": round(update_seconds, 2),
            "unchanged_notes": unchanged.count("unchanged"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=10_000)
    parser.add_argument("--chunks-per-note", type=int, default=10)
    parser.add_argument("--vector-chunks", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(run(args.notes, args.chunks_per_note,
                         args.vector_chunks, args.queries), indent=2))


if __name__ == "__main__":
    main()
