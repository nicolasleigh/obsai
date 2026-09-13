"""Stable retrieval contract shared by future search modes."""

from typing import Protocol
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

JsonScalar = str | int | float | bool | None


class SearchFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    tags: tuple[str, ...] = ()
    folder: str | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    frontmatter: dict[str, JsonScalar] = Field(default_factory=dict)
    dataview: dict[str, str] = Field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(
            self.tags
            or (self.folder and self.folder.strip("/"))
            or self.modified_after
            or self.modified_before
            or self.frontmatter
            or self.dataview
        )


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    note_id: str
    path: str
    title: str
    heading_path: list[str]
    snippet: str
    score: float
    source: str
    sources: tuple[str, ...] = ()


class Retriever(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]: ...
