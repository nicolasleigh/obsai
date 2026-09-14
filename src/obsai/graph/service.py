"""Deterministic, bounded bidirectional graph traversal."""

from collections import deque
from typing import Sequence

from obsai.graph.models import GraphEdge, GraphLimits, GraphNeighborhood, GraphNode
from obsai.graph.repository import GraphRepository
from obsai.errors import VaultError
from obsai.storage.repositories import IndexRepository, NoteRecord


class GraphService:
    def __init__(self, repository: IndexRepository, graph_repository: GraphRepository,
                 *, limits: GraphLimits = GraphLimits()):
        self.notes = repository.notes
        self.links = graph_repository
        self.limits = limits

    def resolve(self, note: str) -> NoteRecord:
        record = self.notes.get_by_path(note) or self.notes.get(note)
        if record is None:
            raise VaultError(f"Unknown indexed note: {note}")
        return record

    def get_backlinks(self, note: str) -> list[GraphEdge]:
        return self.links.backlinks(self.resolve(note).id)

    def get_outgoing_links(self, note: str) -> list[GraphEdge]:
        return self.links.outgoing(self.resolve(note).id)

    def get_neighbors(self, note: str, depth: int | None = None) -> GraphNeighborhood:
        return self.expand([note], depth=min(2, self.limits.max_depth) if depth is None else depth)

    def expand(self, notes: Sequence[str], *, depth: int = 2) -> GraphNeighborhood:
        if depth < 0 or depth > self.limits.max_depth:
            raise ValueError(f"Graph depth must be between 0 and {self.limits.max_depth}")
        nodes: dict[str, GraphNode] = {}
        edges: dict[int, GraphEdge] = {}
        queue: deque[tuple[str, int, str]] = deque()
        truncated = False
        for note in notes:
            record = self.resolve(note)
            if record.id in nodes:
                continue
            if len(nodes) >= self.limits.max_nodes:
                truncated = True
                break
            nodes[record.id] = GraphNode(record.id, record.path, record.title, 0, record.id)
            queue.append((record.id, 0, record.id))
        while queue:
            note_id, distance, seed_id = queue.popleft()
            if distance >= depth:
                continue
            fetch_limit = self.limits.max_edges + 1
            outgoing = self.links.outgoing(note_id, limit=fetch_limit)
            backlinks = self.links.backlinks(note_id, limit=fetch_limit)
            if len(outgoing) == fetch_limit or len(backlinks) == fetch_limit:
                truncated = True
            adjacent = outgoing + backlinks
            for edge in adjacent:
                if edge.id not in edges:
                    if len(edges) >= self.limits.max_edges:
                        truncated = True
                        break
                    edges[edge.id] = edge
                neighbor_id = (edge.target_note_id if edge.source_note_id == note_id
                               else edge.source_note_id)
                if neighbor_id is None or neighbor_id in nodes:
                    continue
                if len(nodes) >= self.limits.max_nodes:
                    truncated = True
                    continue
                record = self.notes.get(neighbor_id)
                if record is None:
                    continue
                nodes[record.id] = GraphNode(record.id, record.path, record.title,
                                             distance + 1, seed_id)
                queue.append((record.id, distance + 1, seed_id))
            if len(edges) >= self.limits.max_edges and queue:
                truncated = True
                break
        return GraphNeighborhood(tuple(nodes.values()), tuple(edges.values()), truncated)
