from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration, overridable via BROWSERBUDDY_* env vars."""

    model_config = SettingsConfigDict(env_prefix="BROWSERBUDDY_")

    data_dir: Path = Path.home() / ".browserbuddy"
    host: str = "127.0.0.1"
    port: int = 8765
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
