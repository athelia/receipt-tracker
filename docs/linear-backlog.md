# Linear backlog seed

Sync seed for the Linear project **Receipt Tracker** (team `AA`). After the issues exist, **Linear is canonical**: edit issues there, not here. See [`linear-issues.md`](linear-issues.md).

Each row became one issue titled `Area: Idea`. Scope and acceptance criteria live in the issue.

## Receipt Tracker

| Area | Idea | Priority | Phase |
|------|------|----------|-------|
| Project scaffold | Set up Python package, FastAPI app, Typer CLI, tests, and git repo | High | v1 |
| Data model | Define SQLite schema for receipts, line items, products, and jobs | High | v1 |
| Golden import | Import the golden workbook as reviewed, human-entered data | High | v1 |
| Watched folder | Ingest new photos from a watched folder into the archive and queue | High | v1 |
| Job queue | Queue extraction jobs for workers that run on other machines | High | v1 |
| Extractor interface | Define a pluggable extractor interface with recorded provenance | High | v1 |
| Vision backend | Extract receipts with a local vision model served by Ollama | High | v1 |
| Model evaluation | Score extractor backends against the golden data and pick a model | Medium | v1 |
| Validation | Flag extracted receipts that fail automatic consistency checks | High | v1 |
| Unit normalisation | Normalise units and pack sizes into comparable unit prices | Medium | v1 |
| Product matching | Match line items to products by code, then by confirmed alias | High | v1 |
| Review queue | Build web UI to review flagged receipts beside their photo | High | v1 |
| Review sampling | Sample unflagged data for review at a rate that tracks error rates | Medium | v1 |
| Users | Add name-picker users with attribution and per-user settings | Medium | v1 |
| CSV export | Export line items with receipt, store, and product data as CSV | Medium | v1 |
| CLI | Add CLI commands for ingest, queue status, re-run, and export | Medium | v1 |
| Photo retention | Apply the 1-3 year, size-capped retention policy to archived photos | Medium | v1 |
| NAS deployment | Package the app as a light Docker image for the NAS | Medium | v1 |
| Database backups | Decide and implement backups for the SQLite database | Low | v1 |
| Price history | Show a product's price over time per store as chart and table | Low | v2 |
| Store comparison | Compare recent prices across stores for a product or basket | Low | v2 |
| Receipt browser | Browse and search receipts with photo and parsed items | Low | v2 |
| JSON export | Export receipts and line items as JSON | Low | v2 |
| Dashboards | Let each user customise their dashboard view | Low | v2 |
| Coupons tax fees | Capture coupons, tax, and fees per item and per receipt | Low | v2 |
| OCR backend | Add a non-GPU extractor backend using local OCR | Low | v2 |
| Mobile capture | Build a phone app that captures receipts straight into ingestion | Low | v3 |
