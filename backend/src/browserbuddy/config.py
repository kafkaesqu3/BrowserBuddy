from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration, overridable via BROWSERBUDDY_* env vars."""

    model_config = SettingsConfigDict(env_prefix="BROWSERBUDDY_")

    data_dir: Path = Path.home() / ".browserbuddy"
    host: str = "127.0.0.1"
    port: int = 8765
    semantic_enabled: bool = False
    embedding_model: str = "sentence-transformers/all-mpnet-base-v2"
    embedding_revision: str = "main"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    auto_digests: bool = False
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    # Max Hamming distance (of 64 bits) between a new screenshot's
    # difference-hash and its page_visit's most recent one to be treated as
    # a duplicate and discarded. 0 disables dedup; 64 keeps only the first
    # screenshot ever taken per page.
    screenshot_dedup_threshold: int = 8

    @property
    def db_path(self) -> Path:
        return self.data_dir / "buddy.db"


def get_settings() -> Settings:
    """Load settings and ensure the data directory exists."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
