"""Shared domain errors."""


class ObsAIError(Exception):
    """Base error for expected ObsAgent failures."""


class ConfigError(ObsAIError):
    """Configuration cannot be loaded or validated."""


class VaultError(ObsAIError):
    """Vault cannot be scanned or read."""


class ParseError(ObsAIError):
    """A Markdown note cannot be parsed safely."""
