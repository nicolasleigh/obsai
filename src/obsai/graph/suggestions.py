"""Read-only semantic and graph-backed WikiLink candidates."""

from dataclasses import dataclass
from pathlib import PurePosixPath, Path

from obsai.graph.service import GraphService
from obsai.retrieval.models import Retriever
from obsai.safe_write.service import SafeWriteService
from obsai.storage.repositories import IndexRepository
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files


@dataclass(frozen=True)
class LinkSuggestion:
    path: str
    title: str
    score: float
    reason: str

    @property
    def wikilink(self) -> str:
        target = self.path[:-3] if self.path.lower().endswith(".md") else self.path
        return f"[[{target}]]"


class LinkSuggester:
    def __init__(self, vault_root: Path, repository: IndexRepository,
                 graph: GraphService, semantic: Retriever):
        self.safe = SafeWriteService(vault_root)
        self.root = self.safe.root
        self.repository = repository
        self.graph = graph
        self.semantic = semantic

    @staticmethod
    def _safe_target(path: str) -> bool:
        return not any(character in path for character in "[]|#\r\n")

    @staticmethod
    def _already_linked(source_path: str, target_path: str, existing: set[str]) -> bool:
        target_no_suffix = target_path[:-3] if target_path.lower().endswith(".md") else target_path
        target_stem = PurePosixPath(target_no_suffix).name
        parent = PurePosixPath(source_path).parent
        for value in existing:
            normalized = value[:-3] if value.lower().endswith(".md") else value
            if normalized in (target_no_suffix, target_stem):
                return True
            if str(parent / normalized) == target_no_suffix:
                return True
        return False

    def suggest(self, path: str, *, limit: int = 10) -> list[LinkSuggestion]:
        if limit <= 0:
            return []
        source = self.safe._path(path)
        note = parse_note(source, vault_root=self.root)
        query = (note.title + "\n" + note.plain_text[:2000]).strip()
        results = self.semantic.search(query, limit=min(100, max(30, limit * 3)))
        indexed = self.repository.notes.get_by_path(note.path)
        neighborhood = self.graph.get_neighbors(indexed.id, depth=2) if indexed else None
        distance_by_id = {item.note_id: item.distance for item in neighborhood.nodes} if neighborhood else {}
        existing = {link.target_path for link in note.wikilinks if link.target_path}
        visible = {item.relative_to(self.root).as_posix() for item in scan_markdown_files(self.root)}
        candidates: dict[str, tuple[float, str]] = {}
        for rank, result in enumerate(results):
            if result.path == note.path or result.path not in visible or not self._safe_target(result.path):
                continue
            if self._already_linked(note.path, result.path, existing):
                continue
            distance = distance_by_id.get(result.note_id)
            score = 1.0 / (rank + 1) + (0.25 / distance if distance else 0.0)
            reason = "Semantic match" + (f"; graph distance {distance}" if distance else "")
            current = candidates.get(result.note_id)
            if current is None or score > current[0]:
                candidates[result.note_id] = (score, reason)
        if neighborhood:
            for node in neighborhood.nodes:
                if node.distance == 0 or node.path not in visible or not self._safe_target(node.path):
                    continue
                if self._already_linked(note.path, node.path, existing):
                    continue
                score, reason = candidates.get(node.note_id, (0.0, ""))
                if score == 0.0:
                    candidates[node.note_id] = (0.15 / node.distance,
                                                f"Graph distance {node.distance}")
        suggestions = []
        for note_id, (score, reason) in candidates.items():
            record = self.repository.notes.get(note_id)
            if record is not None and record.path != note.path:
                suggestions.append(LinkSuggestion(record.path, record.title, score, reason))
        return sorted(suggestions, key=lambda item: (-item.score, item.path))[:limit]
