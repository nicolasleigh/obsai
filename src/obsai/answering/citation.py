"""Validate and normalize model-provided citation IDs.

The wire/UI contract deliberately uses the ASCII ``[S1]`` form.  Local models
occasionally emit the same citation with full-width or round brackets, though;
those are still unambiguous references to a known evidence item and can be
normalized safely.  Unknown IDs remain a hard failure.
"""

import re

from obsai.answering.models import Evidence

_CITATION_VARIANTS = re.compile(
    r"(?:\[(S[0-9]+)\]|【(S[0-9]+)】|〔(S[0-9]+)〕|（(S[0-9]+)）|\((S[0-9]+)\))"
)


def normalize_citations(
    text: str, evidence: tuple[Evidence, ...]
) -> tuple[str, tuple[Evidence, ...]] | None:
    """Return canonical answer text and the evidence citations it uses.

    Only bracketed citation variants are accepted.  In particular, a bare
    ``S1`` is intentionally not treated as a citation because it can occur in
    ordinary prose or a note title.  This keeps the existing citation guarantee
    while accommodating common Ollama output formatting.
    """
    if not text.strip():
        return None

    # Qwen and a few Ollama templates put hidden reasoning in ``<think>``
    # blocks.  It is not an answer and must not provide citations by itself.
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    if not cleaned.strip():
        return None

    available = {item.citation_id: item for item in evidence}
    found: list[str] = []

    def replace(match: re.Match[str]) -> str:
        citation_id = next(group for group in match.groups() if group is not None)
        found.append(citation_id)
        return f"[{citation_id}]"

    normalized = _CITATION_VARIANTS.sub(replace, cleaned)
    if not found or any(citation_id not in available for citation_id in found):
        return None

    sources = tuple(available[citation_id] for citation_id in dict.fromkeys(found))
    return normalized.strip(), sources


def validate_citations(text: str, evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...] | None:
    """Validate citation IDs while preserving the historical public helper."""
    result = normalize_citations(text, evidence)
    return None if result is None else result[1]
