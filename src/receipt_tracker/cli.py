"""Command-line interface."""

import typer

from receipt_tracker import __version__
from receipt_tracker.config import get_settings

app = typer.Typer(
    help="Receipt Tracker: ingest receipt photos and track item prices.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def config() -> None:
    """Show the settings in effect."""
    for name, value in get_settings().model_dump().items():
        typer.echo(f"{name} = {value}")


@app.command()
def migrate() -> None:
    """Create or upgrade the database schema."""
    from receipt_tracker import db

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        applied = db.migrate(conn)
    finally:
        conn.close()
    if not applied:
        typer.echo(f"{settings.db_path}: schema is up to date")
    for migration in applied:
        typer.echo(f"{settings.db_path}: applied {migration.version:04d} {migration.name}")


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Address to bind. Defaults to the HOST setting."),
    port: int | None = typer.Option(None, help="Port to bind. Defaults to the PORT setting."),
) -> None:
    """Run the web app."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "receipt_tracker.web.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
    )
