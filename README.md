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
| `MODEL_SERVER_URL` | `http://localhost:11434` | Local model server (Ollama) |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Web app bind address |

## Household data

Receipts, photos, spreadsheets, and the database are private and are excluded by `.gitignore` (`data/`, `var/`, databases, images, `.xlsx`). Do not force-add them.
