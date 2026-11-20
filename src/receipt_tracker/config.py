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
    # How often `watch` looks for new photos.
    watch_interval_seconds: float = 10.0
    # A photo is picked up once it has gone this long without being modified.
    watch_settle_seconds: float = 5.0
    model_server_url: str = "http://localhost:11434"
    vision_model: str = "qwen2.5vl:7b"  # candidate; AA-56 picks the model
    model_timeout_seconds: float = 300.0
    vision_max_edge: int = 2240
    vision_num_ctx: int = 8192
    # Cut the background away around the receipt before sending the photo to the model.
    vision_crop: bool = True
    # A receipt taller than this multiple of its width is cut into overlapping tiles.
    vision_max_aspect: float = 4.0
    # While waiting for the model, log a "still waiting" line this often (0 turns it off).
    vision_progress_seconds: float = 30.0
    extractor: str | None = None  # backend name; see `receipt_tracker.extractors`
    # Structured event log (JSON lines), one rotating file per process.
    log_dir: Path | None = None
    log_level: str = "INFO"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5
    # Also print each event as a readable line on stderr, to watch a run live.
    log_console: bool = False
    # Also keep the raw model answer of failed extractions, in a separate file.
    # Off by default: the raw answer contains receipt text.
    log_raw_failures: bool = False
    # Model evaluation: which photo shows which golden receipt, and where results are kept.
    golden_manifest: Path = Path("data/photo_manifest.csv")
    eval_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8000

    def model_post_init(self, __context: object) -> None:
        if self.eval_dir is None:
            self.eval_dir = self.data_dir / "eval"
        if self.db_path is None:
            self.db_path = self.data_dir / "receipt_tracker.db"
        if self.watch_dir is None:
            self.watch_dir = self.data_dir / "inbox"
        if self.log_dir is None:
            self.log_dir = self.data_dir / "logs"
        if self.archive_dir is None:
            self.archive_dir = self.data_dir / "archive"


@lru_cache
def get_settings() -> Settings:
    return Settings()
