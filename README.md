# Receipt Tracker

Local household app that turns photos of grocery receipts into line items and tracks item prices over time. Nothing leaves the home network. Background and design: [REQUIREMENTS.md](REQUIREMENTS.md). Work is tracked in Linear (see [docs/linear-issues.md](docs/linear-issues.md)).

## Setup

Needs Python 3.12 or newer.

```bash
make install
```

## Commands

```bash
make check     # lint and tests
make format    # auto-fix lint and formatting
make serve     # run the web app at http://127.0.0.1:8000
make lock      # regenerate requirements*.txt after editing pyproject.toml
```

Tests marked `model` call a real local vision model and are skipped by default. With Ollama running and the model pulled, run them (minutes per case on CPU) with:

```bash
.venv/bin/pytest tests/test_synthetic_model.py --run-model -v
```

The CLI is `.venv/bin/receipt-tracker`; run it with `--help` to list commands.

## Database

SQLite, at `DB_PATH`. Create or upgrade the schema with:

```bash
.venv/bin/receipt-tracker migrate
```

Schema changes are numbered SQL files in `src/receipt_tracker/db/migrations/` (`NNNN_description.sql`). Each is applied once, in order, in its own transaction. Never edit a migration that has been merged; add a new one.

Storage conventions: money is integer cents (`*_cents`), quantities are integer thousandths (`quantity_milli`), percentage discounts are integer basis points (`discount_bp`), and timestamps are UTC ISO-8601 text.

## Configuration

Settings come from environment variables prefixed `RECEIPT_TRACKER_`, or from a `.env` file in the working directory. Copy [.env.example](.env.example) to `.env` to start. `receipt-tracker config` prints the values in effect.

| Setting | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `./var` | Root for everything the app stores |
| `DB_PATH` | `<DATA_DIR>/receipt_tracker.db` | SQLite database |
| `WATCH_DIR` | `<DATA_DIR>/inbox` | Folder watched for new receipt photos |
| `ARCHIVE_DIR` | `<DATA_DIR>/archive` | Managed archive of original photos |
| `WATCH_INTERVAL_SECONDS` | `10` | How often `watch` scans the folder |
| `WATCH_SETTLE_SECONDS` | `5` | How long a photo must sit unmodified before pickup |
| `MODEL_SERVER_URL` | `http://localhost:11434` | Local model server (Ollama) |
| `LOG_DIR` | `<DATA_DIR>/logs` | Event logs: `app.log`, `cli.log`, `worker.log` (JSON lines) |
| `LOG_LEVEL` | `INFO` | Minimum level written to the event log |
| `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | `5000000`, `5` | Rotation size and number of old files kept |
| `LOG_RAW_FAILURES` | `false` | Also keep the raw model answer of failed extractions in `raw-failures.jsonl` (contains receipt text) |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Web app bind address |

## Logs

Each process writes its own JSON-lines file under `LOG_DIR`, one event per line with a UTC timestamp and, for job events, a `job_id`. To follow one job or find failures:

```bash
grep '"job_id": 12' var/logs/*.log
grep '"extraction_failed"' var/logs/worker.log
```

Events include `job_enqueued`, `job_claimed`, `lease_expired`, `job_requeued`, `job_failed`, `job_completed`, and the worker's `extraction_finished`, `extraction_failed` (error type and message) and `extraction_unavailable`. The vision backend adds `model_call` (wall-clock and Ollama's own timing, load time, prompt and output token counts, image sizes, `num_ctx`) for every request, `orientation_failed` and `orientation_used` for the sideways-photo retries, and these carry the job's `job_id` like the rest. A requeue because the model server was unreachable is logged separately from a failure. Raw model output is never in these files; with `LOG_RAW_FAILURES=true` the model's last answer for a failed extraction goes to `raw-failures.jsonl` beside them. Logs stay on the machine and are not committed.

## Adding receipt photos

Put photos in `WATCH_DIR`, then either run one pass or leave the watcher running:

```bash
.venv/bin/receipt-tracker ingest   # one pass
.venv/bin/receipt-tracker watch    # keep scanning; Ctrl-C to stop
```

Each photo is copied into `ARCHIVE_DIR` (as `YYYY/MM/<sha256>.<ext>`), given a receipt and a queued extraction job, and then removed from the watched folder.

- **Formats:** JPEG, PNG, HEIC/HEIF, and WebP. Originals are archived unchanged. Anything else is left in place and reported.
- **Duplicates:** a photo whose content is already archived is removed from the watched folder and nothing new is created.
- **Long receipts:** name the photos `<anything>-1.jpg`, `<anything>-2.jpg` (up to `-9`) and they become one receipt, in that order, as long as they arrive before extraction starts.
- **Half-copied files:** a file modified in the last `WATCH_SETTLE_SECONDS` is left for the next scan.
- Subfolders and hidden files are ignored.

## Importing the golden workbook

Receipts entered by hand before this project live in a workbook under `data/`. Load it once with:

```bash
.venv/bin/receipt-tracker import-golden "data/<workbook>.xlsx" --dry-run
```

Drop `--dry-run` to save. The import creates stores, receipts, and line items, and seeds products, categories, and item codes from the workbook's lookup sheet. Receipts are marked as golden and already reviewed. It prints every row it cleaned up or could not map, and it is safe to run again: receipts already imported are skipped.

The lookup sheet describes one store's item codes. That store is inferred as the one whose rows match the most lookup codes; pass `--lookup-store NAME` to choose it yourself.

## Household data

Receipts, photos, spreadsheets, and the database are private and are excluded by `.gitignore` (`data/`, `var/`, databases, images, `.xlsx`). Do not force-add them.
