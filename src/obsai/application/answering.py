"""Grounded answering over retrieved evidence.

Shares the consent protocol with search: an answer is built from a hybrid
retriever, so it can trigger a remote embedding query and must therefore be
approvable the same way.
"""

from __future__ import annotations

from obsai.answering.context import ContextBuilder
from obsai.answering.models import Answer, EvidenceRecord
from obsai.answering.openai_provider import OpenAILLMProvider
from obsai.answering.ollama_provider import OllamaLLMProvider
from obsai.answering.service import AskService
from obsai.application.dto import (
    AskOutcome,
    AskRequest,
    CitationView,
    ConsentApproval,
    SemanticProbe,
)
from obsai.application.search import (
    SEMANTIC_NOT_APPROVED,
    approval_covers,
    build_semantic_retriever,
    probe_semantic,
)
from obsai.config.models import Settings
from obsai.retrieval import FTSRetriever, HybridRetriever
from obsai.storage.database import Database
from obsai.storage.evidence import SQLiteEvidenceRepository


def citation_location(record: EvidenceRecord) -> str:
    """``path > heading > heading ^block``, the form the CLI and UI both cite."""
    location = record.path
    if record.heading_path:
        location += " > " + " > ".join(record.heading_path)
    if record.block_id:
        location += f" ^{record.block_id}"
    return location


def to_outcome(answer: Answer) -> AskOutcome:
    """Convert the domain answer into the wire contract."""
    return AskOutcome(
        text=answer.text,
        citations=tuple(
            CitationView(
                citation_id=source.citation_id,
                note_id=source.record.note_id,
                chunk_id=source.record.chunk_id,
                path=source.record.path,
                title=source.record.title,
                heading_path=tuple(source.record.heading_path),
                block_id=source.record.block_id,
                location=citation_location(source.record),
                truncated=source.truncated,
            )
            for source in answer.sources
        ),
        warnings=tuple(answer.warnings),
        abstained=answer.abstained,
    )


def build_hybrid_retriever(
    database: Database,
    settings: Settings,
    probe: SemanticProbe,
    approval: ConsentApproval | None = None,
) -> HybridRetriever:
    """Hybrid retriever whose remote semantic half is present only when approved."""
    semantic = None
    reason = probe.reason or SEMANTIC_NOT_APPROVED
    if probe.consent is not None:
        if approval is not None and approval_covers(approval, probe.consent):
            semantic = build_semantic_retriever(database, settings)
        else:
            reason = SEMANTIC_NOT_APPROVED
    elif probe.reason == "" and settings.embedding.provider == "ollama":
        semantic = build_semantic_retriever(database, settings)
    return HybridRetriever(
        FTSRetriever(database), semantic, semantic_unavailable_reason=reason
    )


def build_ask_service(database: Database, settings: Settings, retriever) -> AskService:
    """Assemble the answering service against an open index and a retriever."""
    if settings.ask.provider == "openai":
        provider = OpenAILLMProvider(settings.ask)
    elif settings.ask.provider == "ollama":
        provider = OllamaLLMProvider(settings.ask)
    else:
        from obsai.errors import ConfigError

        raise ConfigError(f"Unsupported answer provider: {settings.ask.provider}")
    return AskService(
        retriever,
        ContextBuilder(SQLiteEvidenceRepository(database), settings.ask),
        provider,
        settings.ask,
    )


def ask(
    request: AskRequest,
    *,
    database: Database,
    settings: Settings,
    probe: SemanticProbe | None = None,
    approval: ConsentApproval | None = None,
) -> AskOutcome:
    """Answer one question from bounded retrieved evidence."""
    if probe is None:
        probe = probe_semantic(request.query, database=database, settings=settings)
    retriever = build_hybrid_retriever(database, settings, probe, approval)
    return to_outcome(build_ask_service(database, settings, retriever).ask(request.query))
