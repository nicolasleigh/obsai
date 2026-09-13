"""Stable retrieval contract shared by future search modes."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class SearchFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    tags: tuple[str, ...] = ()
    folder: str | None = None


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


class Retriever(Protocol):
    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]: ...
