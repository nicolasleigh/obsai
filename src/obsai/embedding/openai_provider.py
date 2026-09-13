"""OpenAI embeddings adapter. Construction and preflight do not make network calls."""

import os

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


class OpenAIEmbeddingProvider:
    def __init__(self, generation: EmbeddingGeneration, timeout_seconds: float):
        if generation.provider != "openai":
            raise ConfigError(f"Unsupported embedding provider: {generation.provider}")
        self.generation = generation
        self.timeout_seconds = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ConfigError("OPENAI_API_KEY is required for remote embeddings")
        async with AsyncOpenAI(
            api_key=key, timeout=self.timeout_seconds, max_retries=0
        ) as client:
            try:
                response = await client.embeddings.create(
                    model=self.generation.model,
                    input=texts,
                    dimensions=self.generation.dimensions,
                    encoding_format="float",
                )
            except RateLimitError as exc:
                raise EmbeddingRateLimitError("OpenAI rate limit (429)") from exc
            except APITimeoutError as exc:
                raise TimeoutError("OpenAI embedding request timed out") from exc
            except APIConnectionError as exc:
                raise ConnectionError("OpenAI embedding network error") from exc
            except APIStatusError as exc:
                if exc.status_code >= 500:
                    raise EmbeddingServiceError(f"OpenAI service error ({exc.status_code})") from exc
                raise EmbeddingError(f"OpenAI rejected embedding request ({exc.status_code})") from exc
        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(texts) or [item.index for item in ordered] != list(range(len(texts))):
            raise EmbeddingServiceError("OpenAI returned incomplete embedding results")
        return [item.embedding for item in ordered]
