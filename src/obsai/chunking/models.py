"""Chunk output and sizing policy."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    min_tokens: int = 80
    target_tokens: int = 260
    max_tokens: int = 400

    def __post_init__(self) -> None:
        if not 0 < self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Require 0 < min_tokens <= target_tokens <= max_tokens")


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    note_id: str
    heading_path: list[str]
    block_id: str | None = None
    raw_content: str
    embedding_text: str
    token_count: int
    content_hash: str
    embedding_text_hash: str
    position: int
    metadata: dict[str, Any] = Field(default_factory=dict)
