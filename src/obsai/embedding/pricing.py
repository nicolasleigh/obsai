"""One place for editable provider cost estimates (USD per million input tokens)."""

from decimal import Decimal

from obsai.errors import ConfigError

# Checked against official OpenAI model pricing on 2026-09-13. Users may override
# these estimates in config.toml because provider prices can change.
OPENAI_EMBEDDING_USD_PER_MILLION = {
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
}


def price_per_million(provider: str, model: str, override: float | None) -> Decimal:
    if override is not None:
        return Decimal(str(override))
    if provider == "openai" and model in OPENAI_EMBEDDING_USD_PER_MILLION:
        return OPENAI_EMBEDDING_USD_PER_MILLION[model]
    raise ConfigError(
        "Unknown embedding price; set embedding.price_per_million_tokens_usd"
    )
