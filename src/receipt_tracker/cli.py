"""Command-line interface."""

from pathlib import Path

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


@app.command("import-golden")
def import_golden(
    workbook: Path = typer.Argument(..., exists=True, dir_okay=False, help="The .xlsx workbook."),
    lookup_store: str | None = typer.Option(
        None, help="Store the lookup sheet's item codes belong to. Inferred when omitted."
    ),
    dry_run: bool = typer.Option(False, help="Report what would be imported; save nothing."),
) -> None:
    """Import the hand-entered golden workbook as reviewed data."""
    from receipt_tracker import db, golden_import

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        try:
            report = golden_import.import_golden(
                conn, workbook, lookup_store=lookup_store, dry_run=dry_run
            )
        except golden_import.WorkbookError as error:
            typer.echo(f"Cannot import {workbook}: {error}", err=True)
            raise typer.Exit(1) from error
    finally:
        conn.close()
    for line in report.lines():
        typer.echo(line)


@app.command()
def ingest() -> None:
    """Move ready photos from the watched folder into the archive and queue them."""
    from receipt_tracker import db
    from receipt_tracker import ingest as ingest_module

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        report = ingest_module.scan(
            conn,
            settings.watch_dir,
            settings.archive_dir,
            settle_seconds=settings.watch_settle_seconds,
        )
    finally:
        conn.close()
    for line in report.lines():
        typer.echo(line)
    if not report.lines():
        typer.echo(f"{settings.watch_dir}: nothing to ingest")


@app.command()
def watch(
    interval: float | None = typer.Option(
        None, min=0.1, help="Seconds between scans. Defaults to WATCH_INTERVAL_SECONDS."
    ),
) -> None:
    """Keep ingesting photos as they arrive in the watched folder. Stop with Ctrl-C."""
    from receipt_tracker import db
    from receipt_tracker import ingest as ingest_module

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        typer.echo(f"Watching {settings.watch_dir}")
        ingest_module.watch(
            lambda: ingest_module.scan(
                conn,
                settings.watch_dir,
                settings.archive_dir,
                settle_seconds=settings.watch_settle_seconds,
            ),
            typer.echo,
            interval=interval or settings.watch_interval_seconds,
        )
    except KeyboardInterrupt:
        typer.echo("Stopped")
    finally:
        conn.close()


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
