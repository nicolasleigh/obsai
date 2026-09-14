"""Read-only graph contracts derived from the Vault's indexed WikiLinks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphLimits:
    max_depth: int = 2
    max_nodes: int = 50
    max_edges: int = 200

    def __post_init__(self) -> None:
        if self.max_depth < 0 or self.max_nodes < 1 or self.max_edges < 0:
            raise ValueError("Graph limits must be nonnegative and allow at least one node")


@dataclass(frozen=True)
class GraphEdge:
    id: int
    source_note_id: str
    source_path: str
    source_block_id: str | None
    target_path: str | None
    target_note_id: str | None
    resolved_target_path: str | None
    target_heading: str | None
    target_block_id: str | None
    display_text: str | None
    is_embed: bool
    position: int

    @property
    def broken(self) -> bool:
        return self.target_note_id is None and self.target_path is not None


@dataclass(frozen=True)
class GraphNode:
    note_id: str
    path: str
    title: str
    distance: int
    seed_note_id: str


@dataclass(frozen=True)
class GraphNeighborhood:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool
