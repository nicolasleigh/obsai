"""Validate model-provided citation IDs before any answer is displayed."""

import re

from obsai.answering.models import Evidence

_CITATION = re.compile(r"\[S[0-9]+\]")


def validate_citations(text: str, evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...] | None:
    available = {f"[{item.citation_id}]": item for item in evidence}
    found = list(dict.fromkeys(_CITATION.findall(text)))
    if not text.strip() or not found or any(label not in available for label in found):
        return None
    return tuple(available[label] for label in found)
