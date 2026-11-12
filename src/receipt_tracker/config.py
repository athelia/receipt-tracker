"""Application settings, read from environment variables and an optional `.env` file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings use the `RECEIPT_TRACKER_` prefix, e.g. `RECEIPT_TRACKER_DATA_DIR`."""

    model_config = SettingsConfigDict(
        env_prefix="RECEIPT_TRACKER_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Path("var")
    db_path: Path | None = None
    watch_dir: Path | None = None
    archive_dir: Path | None = None
    model_server_url: str = "http://localhost:11434"
    host: str = "127.0.0.1"
    port: int = 8000

    def model_post_init(self, __context: object) -> None:
        if self.db_path is None:
            self.db_path = self.data_dir / "receipt_tracker.db"
        if self.watch_dir is None:
            self.watch_dir = self.data_dir / "inbox"
        if self.archive_dir is None:
            self.archive_dir = self.data_dir / "archive"


@lru_cache
def get_settings() -> Settings:
    return Settings()
