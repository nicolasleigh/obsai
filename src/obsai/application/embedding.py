"""Embedding runtime assembly.

The CLI used to build this inline and call ``load_settings()`` from inside, which
made the dependency invisible and unusable from anything but a terminal command.
Settings are now a parameter, so a caller can assemble the pipeline against an
explicit configuration instead of whatever happens to be on disk.
"""

from __future__ import annotations

from obsai.config.models import EmbeddingConfig, Settings
from obsai.embedding.models import EmbeddingGeneration
from obsai.embedding.openai_provider import OpenAIEmbeddingProvider
from obsai.embedding.ollama_provider import OllamaEmbeddingProvider
from obsai.embedding.pipeline import EmbeddingPipeline
from obsai.errors import ConfigError
from obsai.storage.database import Database
from obsai.storage.vectors import SQLiteVectorStore


def build_generation(config: EmbeddingConfig) -> EmbeddingGeneration:
    """Derive the vector-space identity from configuration.

    ``model_version`` falls back to the model name so that the generation hash
    stays stable for providers that do not publish a separate version.
    """
    return EmbeddingGeneration(
        config.provider,
        config.model,
        config.model_version or config.model,
        config.dimensions,
    )


def build_embedding_pipeline(
    database: Database, settings: Settings
) -> tuple[SQLiteVectorStore, EmbeddingPipeline]:
    """Assemble the vector store and its pipeline against an open index."""
    config = settings.embedding
    generation = build_generation(config)
    if config.provider == "openai":
        provider = OpenAIEmbeddingProvider(
            generation, config.timeout_seconds, base_url=config.base_url
        )
    elif config.provider == "ollama":
        provider = OllamaEmbeddingProvider(
            generation, config.timeout_seconds, base_url=config.base_url
        )
    else:
        raise ConfigError(f"Unsupported embedding provider: {config.provider}")
    store = SQLiteVectorStore(database)
    return store, EmbeddingPipeline(store, provider, config)
