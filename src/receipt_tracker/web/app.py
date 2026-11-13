"""FastAPI application: web UI and API."""

from pathlib import Path

from fastapi import FastAPI

from receipt_tracker import __version__, db
from receipt_tracker.config import get_settings
from receipt_tracker.web.jobs import build_router


def create_app(db_path: Path | None = None, archive_dir: Path | None = None) -> FastAPI:
    """Build the app. Paths default to the settings; tests pass their own."""
    app = FastAPI(title="Receipt Tracker", version=__version__)

    migrated = False

    def open_db():
        nonlocal migrated
        conn = db.connect(db_path or get_settings().db_path)
        if not migrated:
            db.migrate(conn)
            migrated = True
        return conn

    def get_archive_dir() -> Path:
        return archive_dir or get_settings().archive_dir

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(build_router(open_db, get_archive_dir))

    return app
