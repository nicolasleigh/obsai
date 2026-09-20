"""Shared configuration helpers for Ollama's OpenAI-compatible local API."""

from __future__ import annotations

import os

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1/"


def ollama_base_url(configured: str | None = None) -> str:
    """Return a normalized Ollama OpenAI-compatible base URL."""
    value = configured or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL
    value = value.strip().rstrip("/")
    if not value:
        value = DEFAULT_OLLAMA_BASE_URL.rstrip("/")
    if not value.endswith("/v1"):
        value += "/v1"
    return value + "/"


def ollama_api_key() -> str:
    """Return the local compatibility key (ignored by a local Ollama server)."""
    return os.environ.get("OLLAMA_API_KEY") or "ollama"
