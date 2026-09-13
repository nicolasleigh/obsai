import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from obsai.chunking import chunk_note
from obsai.cli.app import app
from obsai.config.models import EmbeddingConfig
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.retrieval import FTSRetriever, HybridRetriever, SearchFilters, VectorRetriever
from obsai.retrieval.evaluation import BenchmarkCase, evaluate
from obsai.storage import Database, IndexRepository, SQLiteVectorStore
from obsai.vault.parser import parse_markdown


class BenchmarkProvider:
    generation = EmbeddingGeneration("mock", "benchmark", "v1", 3)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.casefold()
            if "graceful shutdown" in lowered or "平滑退出" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "context" in lowered:
                vectors.append([0.0, 1.0, 0.0])
            elif "sqlite wal" in lowered:
                vectors.append([0.0, 0.0, 1.0])
            else:
                raise AssertionError(f"Unexpected benchmark text: {text}")
        return vectors


def test_project_benchmark_hybrid_is_not_worse_than_single_paths(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "retrieval" / "benchmark.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    cases = [
        BenchmarkCase(case["query"], expected_paths=tuple(case["expected_paths"]))
        for case in data["cases"]
    ]
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        for path, raw in data["notes"].items():
            note = parse_markdown(raw, path)
            index.index_note(note, chunk_note(note))
        store = SQLiteVectorStore(db)
        pipeline = EmbeddingPipeline(
            store, BenchmarkProvider(),
            EmbeddingConfig(price_per_million_tokens_usd=1.0, dimensions=3),
        )
        asyncio.run(pipeline.execute(pipeline.plan(), approved=True))
        keyword = FTSRetriever(db)
        semantic = VectorRetriever(store, pipeline, approved=True)
        hybrid = HybridRetriever(keyword, semantic, candidate_limit=3)
        scores = {name: evaluate(retriever, cases, k=2) for name, retriever in (
            ("keyword", keyword), ("semantic", semantic), ("hybrid", hybrid),
        )}
        assert scores["hybrid"].recall_at_k >= max(
            scores["keyword"].recall_at_k, scores["semantic"].recall_at_k
        )
        assert scores["hybrid"].mrr >= max(scores["keyword"].mrr, scores["semantic"].mrr)
        assert scores["hybrid"].precision_at_k >= max(
            scores["keyword"].precision_at_k, scores["semantic"].precision_at_k
        )
        assert scores["hybrid"].recall_at_k == 1.0
        assert scores["hybrid"].mrr == 1.0
        assert scores["hybrid"].precision_at_k == 0.5
        assert scores["keyword"].recall_at_k == 0.75


def test_metadata_filters_match_in_fts_and_vector(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        index = IndexRepository(db)
        for path, raw in (
            ("Projects/done.md", "---\nstatus: done\nrating: 8\ntags: [work]\n---\n# Done\n\nowner:: alice\n\nShared topic.\n"),
            ("Projects/open.md", "---\nstatus: open\nrating: 3\ntags: [work]\n---\n# Open\n\nowner:: bob\n\nShared topic.\n"),
        ):
            note = parse_markdown(raw, path)
            index.index_note(note, chunk_note(note))
        db.connection.execute(
            "UPDATE notes SET modified_at = ? WHERE path = ?",
            ("2024-01-01T00:00:00+00:00", "Projects/open.md"),
        )
        db.connection.execute(
            "UPDATE notes SET modified_at = ? WHERE path = ?",
            ("2026-01-01T00:00:00+00:00", "Projects/done.md"),
        )
        store = SQLiteVectorStore(db)
        generation = EmbeddingGeneration("mock", "metadata", "v1", 2)
        for path in ("Projects/done.md", "Projects/open.md"):
            chunk = index.chunks.list_for_note(index.notes.get_by_path(path).id)[0]
            store.upsert(
                generation, chunk.chunk_id, chunk.embedding_text_hash,
                [1.0, 0.0] if path.endswith("done.md") else [0.0, 1.0], 10,
            )
        filters = SearchFilters(
            folder="Projects", tags=("work",),
            modified_after=datetime(2025, 1, 1, tzinfo=timezone.utc),
            modified_before=datetime(2027, 1, 1, tzinfo=timezone.utc),
            frontmatter={"status": "done", "rating": 8},
            dataview={"owner": "alice"},
        )
        assert [r.path for r in FTSRetriever(db).search("Shared topic", filters=filters)] == ["Projects/done.md"]
        assert [r.path for r in store.search(generation, [1.0, 0.0], filters=filters)] == ["Projects/done.md"]
        assert FTSRetriever(db).search(
            "Shared topic", filters=filters.model_copy(update={"frontmatter": {"status": "missing"}})
        ) == []
        assert store.search(
            generation, [1.0, 0.0],
            filters=filters.model_copy(update={"dataview": {"owner": "nobody"}}),
        ) == []


def test_default_cli_hybrid_warns_when_vector_index_is_missing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nKeyword answer.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{database_path}"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    result = runner.invoke(app, ["search", "Keyword answer", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["source"] == "hybrid"
    assert "Semantic index missing" in result.stderr
    strict = runner.invoke(app, ["search", "Keyword answer", "--strict-semantic"])
    assert strict.exit_code != 0


def test_cli_metadata_options_apply_to_keyword_search(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Projects").mkdir(parents=True)
    (vault / "Projects" / "done.md").write_text(
        "---\nstatus: done\nrating: 8\ntags: [work]\n---\n# Done\n\nowner:: alice\n\nShared topic.\n",
        encoding="utf-8",
    )
    (vault / "Projects" / "open.md").write_text(
        "---\nstatus: open\nrating: 3\ntags: [work]\n---\n# Open\n\nowner:: bob\n\nShared topic.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    config_path = tmp_path / "config" / "obsai" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{db_path}"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["index", "update"]).exit_code == 0
    result = runner.invoke(app, [
        "search", "Shared topic", "--mode", "keyword", "--folder", "Projects",
        "--tag", "work", "--frontmatter", "status=done",
        "--frontmatter", "rating=8", "--dataview", "owner=alice",
        "--modified-after", "2020-01-01T00:00:00+00:00", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert [item["path"] for item in json.loads(result.stdout)] == ["Projects/done.md"]
    invalid = runner.invoke(app, ["search", "x", "--frontmatter", "bad-format"])
    assert invalid.exit_code != 0
