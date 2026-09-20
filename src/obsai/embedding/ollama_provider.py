"""Ollama embedding adapter through its OpenAI-compatible API."""

from __future__ import annotations

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from obsai.embedding.models import EmbeddingGeneration
from obsai.errors import (
    ConfigError,
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingServiceError,
)
from obsai.ollama import ollama_api_key, ollama_base_url


class OllamaEmbeddingProvider:
    """Generate local vectors without an OpenAI credential or external network call."""

    def __init__(
        self,
        generation: EmbeddingGeneration,
        timeout_seconds: float,
        base_url: str | None = None,
    ) -> None:
        if generation.provider != "ollama":
            raise ConfigError(f"Unsupported embedding provider: {generation.provider}")
        self.generation = generation
        self.timeout_seconds = timeout_seconds
        self.base_url = ollama_base_url(base_url)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch through ``POST /v1/embeddings`` on Ollama."""
        if not texts:
            return []

        async with AsyncOpenAI(
            base_url=self.base_url,
            api_key=ollama_api_key(),
            timeout=self.timeout_seconds,
            max_retries=0,
        ) as client:
            try:
                response = await client.embeddings.create(
                    model=self.generation.model,
                    input=texts,
                    dimensions=self.generation.dimensions,
                    encoding_format="float",
                )
            except RateLimitError as exc:
                raise EmbeddingRateLimitError("Ollama embedding rate limit (429)") from exc
            except APITimeoutError as exc:
                raise TimeoutError("Ollama embedding request timed out") from exc
            except APIConnectionError as exc:
                raise ConnectionError("Ollama embedding server is unavailable") from exc
            except APIStatusError as exc:
                if exc.status_code >= 500:
                    raise EmbeddingServiceError(
                        f"Ollama embedding service error ({exc.status_code})"
                    ) from exc
                raise EmbeddingError(
                    f"Ollama rejected embedding request ({exc.status_code})"
                ) from exc

        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(texts) or [item.index for item in ordered] != list(range(len(texts))):
            raise EmbeddingServiceError("Ollama returned incomplete embedding results")
        return [item.embedding for item in ordered]
