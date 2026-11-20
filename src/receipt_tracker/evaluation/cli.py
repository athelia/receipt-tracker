"""The `evaluate` commands: run a backend over the golden photos, then read the results."""

from pathlib import Path

import typer

from receipt_tracker import db, logging_setup
from receipt_tracker.config import Settings, get_settings
from receipt_tracker.evaluation import report, runner, runs, truth
from receipt_tracker.extractors import create
from receipt_tracker.extractors.base import BackendUnavailable

app = typer.Typer(
    help="Score extractor backends against the golden receipts.", no_args_is_help=True
)

# Only these settings describe how a backend was run, so only they are kept with a run.
RECORDED_PREFIXES = ("model_", "vision_")


def with_overrides(base: Settings, overrides: list[str]) -> Settings:
    """`base` with `name=value` overrides applied; unknown names are an error."""
    changes = {}
    for override in overrides:
        name, separator, value = override.partition("=")
        if not separator or name not in Settings.model_fields:
            raise typer.BadParameter(f"{override!r}: expected NAME=VALUE with a known setting name")
        changes[name] = value
    return Settings.model_validate({**base.model_dump(), **changes})


def recorded(settings: Settings) -> dict:
    return {
        name: value
        for name, value in settings.model_dump(mode="json").items()
        if name.startswith(RECORDED_PREFIXES)
    }


def load_cases(settings: Settings, manifest: Path, stores: list[str]) -> truth.Cases:
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        golden = truth.load_truth(conn)
    finally:
        conn.close()
    if not golden:
        typer.echo("No golden receipts in the database: run `import-golden` first.", err=True)
        raise typer.Exit(1)
    paired = truth.pair_photos(manifest, golden)
    if stores:
        wanted = {name.lower() for name in stores}
        paired = truth.Cases(
            tuple(c for c in paired.cases if c.truth.store.lower() in wanted), paired.skipped
        )
    return paired


@app.command()
def run(
    backend: str = typer.Argument(..., help="Backend name, e.g. ollama-vision."),
    set_: list[str] = typer.Option(
        [], "--set", help="Override a setting for this run, e.g. --set vision_model=llava:7b."
    ),
    label: str | None = typer.Option(None, help="A short name added to the run id."),
    resume: str | None = typer.Option(None, help="Continue this run; receipts done are skipped."),
    store: list[str] = typer.Option([], help="Only receipts from this store (repeatable)."),
    limit: int | None = typer.Option(None, min=1, help="Only the first N receipts."),
    manifest: Path | None = typer.Option(None, help="Defaults to the GOLDEN_MANIFEST setting."),
) -> None:
    """Read each golden photo with BACKEND, score it, and store the results.

    Each receipt is saved as it finishes, so a run that is stopped (or loses the model
    server) can be continued with --resume.
    """
    settings = with_overrides(get_settings(), set_)
    logging_setup.configure(settings, "evaluate", console=True)
    paired = load_cases(settings, manifest or settings.golden_manifest, store)
    cases = paired.cases[:limit]
    if not cases:
        typer.echo("No golden receipt has a photo to read.", err=True)
        raise typer.Exit(1)
    for reason in paired.skipped:
        typer.echo(f"skipped {reason}")

    if resume:
        current = runs.open_run(settings.eval_dir, resume)
        if current.meta["backend"] != backend or current.meta["settings"] != recorded(settings):
            typer.echo(f"{resume} was run with other settings; start a new run.", err=True)
            raise typer.Exit(1)
    else:
        meta = {"backend": backend, "settings": recorded(settings), "receipts": len(cases)}
        current = runs.create_run(
            settings.eval_dir, runs.new_run_id(backend, label), meta | {"label": label}
        )
    typer.echo(f"run {current.run_id}: {len(cases)} receipts")

    extractor = create(backend, settings)
    probe = (
        runner.ollama_vram_probe(settings.model_server_url, settings.vision_model)
        if backend == "ollama-vision"
        else None
    )

    def progress(index: int, total: int, record: dict) -> None:
        lines = record["counts"]["extracted_lines"]
        typer.echo(
            f"[{index}/{total}] {record['key']}: {record['status']} in {record['seconds']:.0f}s, "
            f"{lines}/{record['counts']['truth_lines']} lines"
        )

    try:
        runner.evaluate(extractor, cases, current, probe=probe, progress=progress)
    except BackendUnavailable as error:
        typer.echo(f"Stopped: {error}. Continue with --resume {current.run_id}", err=True)
        raise typer.Exit(1) from error
    typer.echo("")
    typer.echo(report.render_run(current))


@app.command()
def show(run_id: str = typer.Argument(..., help="A run id, see `evaluate list`.")) -> None:
    """Print the tables for a stored run."""
    typer.echo(report.render_run(runs.open_run(get_settings().eval_dir, run_id)))


@app.command("list")
def list_() -> None:
    """List stored runs."""
    for stored in runs.list_runs(get_settings().eval_dir):
        done = len(stored.done_keys())
        typer.echo(f"{stored.run_id}  {done}/{stored.meta.get('receipts', '?')} receipts")


@app.command()
def compare(
    run_ids: list[str] = typer.Argument(..., help="Two or more run ids."),
    store: str = typer.Option(report.ALL, help="One store, or ALL."),
) -> None:
    """Put runs side by side, over the receipts they all cover."""
    root = get_settings().eval_dir
    try:
        typer.echo(report.render_comparison([runs.open_run(root, i) for i in run_ids], store))
    except (FileNotFoundError, KeyError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


@app.command()
def rescore(run_id: str = typer.Argument(...)) -> None:
    """Score a run's stored answers again, after the scoring rules changed."""
    settings = get_settings()
    stored = runs.open_run(settings.eval_dir, run_id)
    conn = db.connect(settings.db_path)
    try:
        golden = {t.key: t for t in truth.load_truth(conn)}
    finally:
        conn.close()
    typer.echo(f"{runner.rescore(stored, golden)} receipts rescored")
