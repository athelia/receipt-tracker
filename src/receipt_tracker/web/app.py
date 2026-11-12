"""FastAPI application: web UI and API."""

from fastapi import FastAPI

from receipt_tracker import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="Receipt Tracker", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
