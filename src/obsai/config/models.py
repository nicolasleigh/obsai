"""Phase 0 configuration fields."""

from pathlib import Path
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class VaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path | None = None


class IndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: Path | None = None


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "text-embedding-3-small"
    model_version: str | None = None
    dimensions: int = Field(default=1536, gt=0)
    batch_size: int = Field(default=64, gt=0, le=2048)
    max_concurrency: int = Field(default=2, gt=0)
    max_input_tokens: int = Field(default=8192, gt=0, le=8192)
    max_request_tokens: int = Field(default=300000, gt=0, le=300000)
    timeout_seconds: float = Field(default=30, gt=0)
    max_attempts: int = Field(default=4, gt=0)
    max_embedding_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_limit_usd: float | None = Field(default=None, ge=0)
    max_embedding_requests: int | None = Field(default=None, ge=0)
    price_per_million_tokens_usd: float | None = Field(default=None, ge=0)


class AskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    timeout_seconds: float = Field(default=60, gt=0)
    max_output_tokens: int = Field(default=1024, gt=0)
    max_context_tokens: int = Field(default=12000, gt=0)
    max_evidence_tokens: int = Field(default=2500, gt=0)
    max_chunks: int = Field(default=6, gt=0)


class OrganizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inbox: str = "Inbox"

    @field_validator("inbox")
    @classmethod
    def valid_inbox(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = PurePosixPath(value).parts
        if (not value or value == "." or value.startswith("/") or "\\" in value
                or any(part in (".", "..") for part in parts)):
            raise ValueError("inbox must be a safe Vault-relative directory")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBSAI_", env_nested_delimiter="__", extra="forbid"
    )

    vault: VaultConfig = Field(default_factory=VaultConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    ask: AskConfig = Field(default_factory=AskConfig)
    organize: OrganizeConfig = Field(default_factory=OrganizeConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment values can override individual fields from config.toml.
        return env_settings, init_settings, dotenv_settings, file_secret_settings
