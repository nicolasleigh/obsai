"""WikiLink graph, bounded expansion, retrieval, and suggestion behavior."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obsai.cli.app import app
from obsai.errors import ConfigError
from obsai.graph import GraphLimits, GraphRepository, GraphService, LinkSuggester
from obsai.indexing import IncrementalIndexer
from obsai.retrieval import FTSRetriever, GraphRetriever, HybridRetriever, SearchFilters, SearchResult
from obsai.storage import Database, IndexRepository


def fixture(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text(
        "---\ntags: [shared]\n---\n# Alpha\n\nalpha-only seed. [[B#Deep]] [[B#^b-ref]] [[Missing]]\n", encoding="utf-8"
    )
    (vault / "B.md").write_text(
        "# Beta\n\n## Intro\n\nIntroduction.\n\n## Deep\n\nHidden adjacent knowledge. ^b-ref\n\n[[C]]\n",
        encoding="utf-8",
    )
    (vault / "C.md").write_text("# Gamma\n\n[[A]]\n", encoding="utf-8")
    (vault / "D.md").write_text("# Delta\n\nIsolated.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    database = Database(database_path)
    repository = IndexRepository(database)
    IncrementalIndexer(repository).update(vault)
    graph = GraphService(repository, GraphRepository(database))
    return vault, database, repository, graph


def test_outgoing_backlinks_cycle_broken_and_block_links(tmp_path):
    _, database, repository, graph = fixture(tmp_path)
    outgoing = graph.get_outgoing_links("A.md")
    assert len(outgoing) == 3
    assert any(edge.target_heading == "Deep" and edge.target_note_id for edge in outgoing)
    assert any(edge.target_block_id == "b-ref" and edge.target_note_id for edge in outgoing)
    assert next(edge for edge in outgoing if edge.target_path == "Missing").broken
    backlinks = graph.get_backlinks("B.md")
    assert len(backlinks) == 2 and {edge.source_path for edge in backlinks} == {"A.md"}
    one_hop = graph.get_neighbors("A.md", depth=1)
    assert {node.path for node in one_hop.nodes} == {"A.md", "B.md", "C.md"}
    assert "Missing" not in {node.path for node in one_hop.nodes}
    two_hop = graph.get_neighbors("A.md", depth=2)
    assert {node.path for node in two_hop.nodes} == {"A.md", "B.md", "C.md"}
    assert len(two_hop.edges) == len({edge.id for edge in two_hop.edges})
    assert repository.notes.get_by_path("A.md").id == graph.resolve("A.md").id
    database.close()


def test_depth_and_graph_explosion_limits(tmp_path):
    _, database, repository, _ = fixture(tmp_path)
    graph = GraphService(repository, GraphRepository(database),
                         limits=GraphLimits(max_depth=2, max_nodes=2, max_edges=2))
    neighborhood = graph.get_neighbors("A.md", depth=2)
    assert len(neighborhood.nodes) <= 2
    assert len(neighborhood.edges) <= 2
    assert neighborhood.truncated
    assert {node.path for node in graph.get_neighbors("A.md", depth=0).nodes} == {"A.md"}
    with pytest.raises(ValueError):
        graph.get_neighbors("A.md", depth=3)
    edge_bounded = GraphService(repository, GraphRepository(database),
                                limits=GraphLimits(max_depth=2, max_nodes=50, max_edges=1))
    limited = edge_bounded.get_neighbors("A.md")
    assert len(limited.edges) <= 1 and limited.truncated
    database.close()


def test_graph_retriever_finds_neighbor_missed_by_seed_search(tmp_path):
    _, database, repository, graph = fixture(tmp_path)
    seed = FTSRetriever(database)
    assert {item.path for item in seed.search("alpha-only")} == {"A.md"}
    retriever = GraphRetriever(seed, graph, repository)
    results = retriever.search("alpha-only", limit=10)
    assert results[0].path == "A.md"
    neighbor = next(item for item in results if item.path == "B.md")
    assert neighbor.source == "graph"
    assert neighbor.heading_path[-1] == "Deep"
    assert len({item.chunk_id for item in results}) == len(results)
    assert retriever.search(" ") == []
    filtered = retriever.search("alpha-only", filters=SearchFilters(tags=("shared",)))
    assert {item.path for item in filtered} == {"A.md"}
    database.close()


def test_semantic_seed_expands_to_adjacent_knowledge(tmp_path):
    _, database, repository, graph = fixture(tmp_path)
    note = repository.notes.get_by_path("A.md")
    chunk = repository.chunks.list_for_note(note.id)[0]
    semantic = FakeSemantic([SearchResult(
        chunk_id=chunk.chunk_id, note_id=note.id, path=note.path, title=note.title,
        heading_path=chunk.heading_path, snippet="alpha-only", score=0.9,
        source="semantic",
    )])
    empty_keyword = FakeSemantic([])
    hybrid = HybridRetriever(empty_keyword, semantic)
    results = GraphRetriever(hybrid, graph, repository).search("natural-language query")
    assert {item.path for item in semantic.results} == {"A.md"}
    assert "B.md" in {item.path for item in results}
    database.close()


def test_rename_does_not_leave_a_false_graph_edge(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\n[[B]]\n")
    (vault / "B.md").write_text("# B\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        repository = IndexRepository(database)
        updater = IncrementalIndexer(repository)
        updater.update(vault)
        assert GraphService(repository, GraphRepository(database)).get_outgoing_links("A.md")[0].target_note_id
        (vault / "B.md").rename(vault / "C.md")
        updater.update(vault)
        graph = GraphService(repository, GraphRepository(database))
        assert graph.get_outgoing_links("A.md")[0].broken
        assert {node.path for node in graph.get_neighbors("A.md").nodes} == {"A.md"}


class FakeSemantic:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query, limit=10, filters=None):
        self.calls.append(query)
        return self.results[:limit]


def test_link_suggestion_filters_existing_links_and_never_writes(tmp_path):
    vault, database, repository, graph = fixture(tmp_path)
    by_path = {record.path: record for record in repository.notes.list_all()}
    results = []
    for path in ("B.md", "C.md", "A.md"):
        note = by_path[path]
        chunk = repository.chunks.list_for_note(note.id)[0]
        results.append(SearchResult(
            chunk_id=chunk.chunk_id, note_id=note.id, path=path, title=note.title,
            heading_path=chunk.heading_path, snippet="", score=0.8, source="semantic",
        ))
    semantic = FakeSemantic(results)
    before = (vault / "A.md").read_bytes()
    suggestions = LinkSuggester(vault, repository, graph, semantic).suggest("A.md")
    assert [item.path for item in suggestions] == ["C.md"]
    assert suggestions[0].wikilink == "[[C]]"
    assert "graph distance" in suggestions[0].reason
    assert (vault / "A.md").read_bytes() == before
    assert semantic.calls and "Alpha" in semantic.calls[0]
    database.close()


def test_links_cli_and_graph_search(tmp_path, monkeypatch):
    vault, database, repository, _ = fixture(tmp_path)
    config = tmp_path / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{tmp_path / "index.db"}"\n'
    )
    runner = CliRunner()
    outgoing = runner.invoke(app, ["links", "outgoing", "A.md"])
    assert outgoing.exit_code == 0 and "Missing [unresolved]" in outgoing.output
    backlinks = runner.invoke(app, ["links", "backlinks", "B.md"])
    assert backlinks.exit_code == 0 and "A.md" in backlinks.output
    related = runner.invoke(app, ["links", "related", "A.md", "--depth", "1"])
    assert related.exit_code == 0 and "B.md" in related.output
    graph_search = runner.invoke(app, ["search", "alpha-only", "--mode", "graph", "--json"])
    assert graph_search.exit_code == 0, graph_search.output
    assert '"source": "graph"' in graph_search.output
    unavailable = runner.invoke(app, ["links", "suggest", "A.md"])
    assert isinstance(unavailable.exception, ConfigError)

    import importlib
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from obsai.application import search as search_module
    from obsai.application.dto import ConsentApproval, RemoteConsent, SemanticProbe

    cli_module = importlib.import_module("obsai.cli.app")
    note = repository.notes.get_by_path("C.md")
    chunk = repository.chunks.list_for_note(note.id)[0]
    semantic = FakeSemantic([SearchResult(
        chunk_id=chunk.chunk_id, note_id=note.id, path=note.path, title=note.title,
        heading_path=chunk.heading_path, snippet="", score=0.9, source="semantic",
    )])
    # Consent moved out of the CLI into the application layer, so the seam is now
    # "the user approved this challenge" plus "build the approved retriever".
    now = datetime.now(timezone.utc)
    consent = RemoteConsent(
        consent_id="test-consent", query_hash="test-hash", generation_id="test-generation",
        expires_at=now + timedelta(minutes=5), query_tokens=1, estimated_cost_usd=Decimal("0"),
    )
    approval = ConsentApproval(
        consent_id="test-consent", query_hash="test-hash", generation_id="test-generation",
        nonce="test-nonce", approved_at=now,
    )
    monkeypatch.setattr(
        cli_module, "_approve_remote", lambda *args, **kwargs: (SemanticProbe(consent=consent), approval)
    )
    monkeypatch.setattr(search_module, "build_semantic_retriever", lambda *args, **kwargs: semantic)
    before = (vault / "A.md").read_bytes()
    suggestion = runner.invoke(app, ["links", "suggest", "A.md"])
    assert suggestion.exit_code == 0, suggestion.output
    assert "[[C]]" in suggestion.output
    assert (vault / "A.md").read_bytes() == before
    declined = runner.invoke(app, ["links", "suggest", "A.md", "--apply"], input="1\nn\n")
    assert declined.exit_code == 0, declined.output
    assert (vault / "A.md").read_bytes() == before
    approved = runner.invoke(app, ["links", "suggest", "A.md", "--apply"], input="1\ny\n")
    assert approved.exit_code == 0, approved.output
    assert "[[C]]" in (vault / "A.md").read_text()
    database.close()
