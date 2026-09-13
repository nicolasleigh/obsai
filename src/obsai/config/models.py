"""Phase 0 configuration fields."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class VaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path | None = None


class IndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: Path | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBSAI_", env_nested_delimiter="__", extra="forbid"
    )

    vault: VaultConfig = Field(default_factory=VaultConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)

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
