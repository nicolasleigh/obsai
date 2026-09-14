"""Shared domain errors."""


class ObsAIError(Exception):
    """Base error for expected ObsAgent failures."""


class ConfigError(ObsAIError):
    """Configuration cannot be loaded or validated."""


class VaultError(ObsAIError):
    """Vault cannot be scanned or read."""


class ParseError(ObsAIError):
    """A Markdown note cannot be parsed safely."""


class SchemaError(ObsAIError):
    """SQLite schema is missing or incompatible."""


class EmbeddingError(ObsAIError):
    """Embedding generation, storage, or retrieval failed."""


class EmbeddingBudgetError(EmbeddingError):
    """The preflight or live request budget would be exceeded."""


class EmbeddingRateLimitError(EmbeddingError):
    """A provider temporarily rejected a request for rate limiting."""


class EmbeddingServiceError(EmbeddingError):
    """A provider temporarily failed with a server error."""


class ContextError(ObsAIError):
    """Evidence cannot fit within the configured context budget."""


class LLMError(ObsAIError):
    """The answer provider could not generate a response."""


class SafeWriteError(ObsAIError):
    """A proposed Vault edit is invalid or cannot be committed safely."""


class ConflictError(SafeWriteError):
    """The source changed or disappeared since the change was prepared."""


class CollisionError(SafeWriteError):
    """A destination already exists."""


class InvalidEncodingError(SafeWriteError):
    """A note is not valid UTF-8 text."""
