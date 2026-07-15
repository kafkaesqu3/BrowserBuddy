from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration, overridable via BROWSERBUDDY_* env vars."""

    model_config = SettingsConfigDict(env_prefix="BROWSERBUDDY_")

    data_dir: Path = Path.home() / ".browserbuddy"
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.data_dir / "buddy.db"


def get_settings() -> Settings:
    """Load settings and ensure the data directory exists."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
