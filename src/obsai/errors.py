"""Shared domain errors."""


class ObsAIError(Exception):
    """Base error for expected ObsAgent failures."""


class ConfigError(ObsAIError):
    """Configuration cannot be loaded or validated."""
