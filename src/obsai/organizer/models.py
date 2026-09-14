"""Reviewable Inbox proposals; absence of a destination means leave in place."""

from dataclasses import dataclass
import re
from pathlib import PurePosixPath


@dataclass(frozen=True)
class RelatedLink:
    path: str
    title: str

    @property
    def wikilink(self) -> str:
        target = self.path[:-3] if self.path.lower().endswith(".md") else self.path
        title = re.sub(r"[\[\]|\r\n]", " ", self.title).strip() or PurePosixPath(target).name
        return f"[[{target}|{title}]]"


@dataclass(frozen=True)
class OrganizerProposal:
    path: str
    destination: str | None
    title: str
    add_tags: tuple[str, ...]
    add_links: tuple[RelatedLink, ...]
    affected_backlinks: tuple[str, ...]
    confidence: float
    reason: str
    issue: str | None = None

    @property
    def actionable(self) -> bool:
        return self.destination is not None and self.issue is None

    @property
    def selected_by_default(self) -> bool:
        return self.actionable and self.confidence >= 0.70
