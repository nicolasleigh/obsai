"""Hybrid seeds expanded through bounded WikiLink neighbors."""

import re

from obsai.graph import GraphNeighborhood, GraphService
from obsai.retrieval.models import Retriever, SearchFilters, SearchResult
from obsai.storage.repositories import IndexRepository
from obsai.telemetry import measured


class GraphRetriever:
    def __init__(self, seeds: Retriever, graph: GraphService, repository: IndexRepository,
                 *, seed_limit: int = 12, depth: int = 2):
        if seed_limit < 1 or depth < 0 or depth > graph.limits.max_depth:
            raise ValueError("Invalid graph retrieval limits")
        self.seeds = seeds
        self.graph = graph
        self.repository = repository
        self.seed_limit = seed_limit
        self.depth = depth
        self.last_warnings: tuple[str, ...] = ()
        self.last_neighborhood: GraphNeighborhood | None = None

    @staticmethod
    def _best_chunk(repository: IndexRepository, note_id: str, query: str,
                    headings: set[str], block_ids: set[str]):
        chunks = repository.chunks.list_for_note(note_id)
        if not chunks:
            return None
        terms = set(re.findall(r"[^\W_]+", query.lower(), re.UNICODE))

        def rank(chunk):
            heading = " > ".join(chunk.heading_path).lower()
            content = chunk.embedding_text.lower()
            return (
                8 if chunk.block_id and chunk.block_id in block_ids else 0,
                5 if any(item.lower() in heading for item in headings) else 0,
                sum(term in content for term in terms),
                -chunk.position,
            )

        return max(chunks, key=rank)

    @measured("retrieval.graph")
    def search(self, query: str, limit: int = 10,
               filters: SearchFilters | None = None) -> list[SearchResult]:
        self.last_neighborhood = None
        self.last_warnings = ()
        if not query.strip() or limit <= 0:
            return []
        seed_count = min(max(limit, self.seed_limit), self.graph.limits.max_nodes)
        seeds = self.seeds.search(query, limit=seed_count, filters=filters)[:seed_count]
        self.last_warnings = tuple(getattr(self.seeds, "last_warnings", ()))
        if not seeds:
            return []
        ranked_seed_notes: dict[str, int] = {}
        seed_results: list[SearchResult] = []
        for rank, item in enumerate(seeds):
            ranked_seed_notes.setdefault(item.note_id, rank)
            seed_results.append(item.model_copy(update={"score": 1.0 / (rank + 1)}))
        neighborhood = self.graph.expand(list(ranked_seed_notes), depth=self.depth)
        self.last_neighborhood = neighborhood
        by_chunk = {item.chunk_id: item for item in seed_results}
        for node in neighborhood.nodes:
            if node.distance == 0 or node.note_id in ranked_seed_notes:
                continue
            if not self.graph.links.note_matches(node.note_id, filters):
                continue
            touching = [edge for edge in neighborhood.edges
                        if edge.target_note_id == node.note_id or edge.source_note_id == node.note_id]
            headings = {edge.target_heading for edge in touching
                        if edge.target_note_id == node.note_id and edge.target_heading}
            block_ids = {edge.target_block_id for edge in touching
                         if edge.target_note_id == node.note_id and edge.target_block_id}
            block_ids.update(edge.source_block_id for edge in touching
                             if edge.source_note_id == node.note_id and edge.source_block_id)
            chunk = self._best_chunk(self.repository, node.note_id, query, headings, block_ids)
            if chunk is None:
                continue
            seed_rank = ranked_seed_notes.get(node.seed_note_id, len(seeds))
            score = 1.0 / ((seed_rank + 1) * (node.distance + 1))
            by_chunk.setdefault(chunk.chunk_id, SearchResult(
                chunk_id=chunk.chunk_id, note_id=node.note_id, path=node.path,
                title=node.title, heading_path=list(chunk.heading_path),
                snippet=chunk.raw_content[:240], score=score, source="graph",
                sources=("graph",),
            ))
        return sorted(by_chunk.values(), key=lambda item: (-item.score, item.path, item.chunk_id))[:limit]
