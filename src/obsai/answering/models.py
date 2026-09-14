"""Provider-neutral answer and evidence contracts."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EvidenceRecord:
    chunk_id: str
    note_id: str
    path: str
    title: str
    heading_path: tuple[str, ...]
    block_id: str | None
    raw_content: str


class EvidenceRepository(Protocol):
    def get(self, chunk_id: str) -> EvidenceRecord | None: ...


@dataclass(frozen=True)
class Evidence:
    citation_id: str
    record: EvidenceRecord
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class Context:
    system_prompt: str
    user_prompt: str
    evidence: tuple[Evidence, ...]
    token_estimate: int


class LLMProvider(Protocol):
    async def generate(
        self, system_prompt: str, user_prompt: str, *, max_output_tokens: int
    ) -> str: ...


@dataclass(frozen=True)
class Answer:
    text: str
    sources: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    abstained: bool = False
