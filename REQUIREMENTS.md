# Receipt Tracker — Requirements

Status: draft for review (2026-11-12). Gathered by interview; items marked **Open** still need a decision.

Household-specific details (store names, exact hardware, real receipt examples) are kept out of this file because the repository is public. They live in `REQUIREMENTS.local.md`, which is ignored by git.

## 1. Purpose

Track grocery prices at the item level for one household. Photos of paper receipts are turned into structured line items, matched to canonical products, and used to see how prices change over time and between stores.

Not a goal: budgeting, tax records, or expense reimbursement.

## 2. Constraints

- **Nothing leaves the home network.** No cloud vision or cloud OCR. Sending locally extracted *text* to a hosted model is a possible later option, never images.
- Local household use only; never deployed publicly.
- Non-technical household members must be able to use it without running anything themselves.
- v1 scope: English-language receipts in US dollars, designed and tested for groceries. Other retail receipts are accepted but not tuned for.

## 3. Architecture

| Machine | Role |
|---|---|
| App host — an always-on, low-power NAS running Docker | Web UI, API, SQLite database, watched folder, photo archive, job queue. No OCR or model work: the NAS has other duties, so the app must stay light. |
| Model host — a PC with a GPU | Local model server (Ollama with a vision model), reached over the LAN |
| Development machine | Development; fallback extraction worker; host for any non-GPU backend |

- The model host's GPU memory limits how large a vision model can be. The specific model is chosen by testing candidates on real receipts.
- Extraction runs in a **worker** that is separate from the app. A worker claims queued receipts, calls an extractor backend, and writes results back.
- Receipts wait in the queue when no worker or model server is reachable. The UI shows queue state.
- **Extractor is pluggable.** Backends, in planned order:
  1. Local vision model on the model host (v1)
  2. Local OCR + local text model, or OCR + rules (runs on the development machine without the GPU; not on the NAS)
  3. Local OCR + hosted text model (optional, text only)
- Every extraction records which backend and model version produced it, so results can be compared and re-run.

**Stack:** Python, FastAPI (web/API), Typer (CLI), SQLite, Docker image for the NAS.

## 4. Phases

### v1

**Ingestion**
- Photos are placed manually in a watched folder; new files are picked up automatically.
- CLI commands for the same pipeline: ingest a file or folder, show queue status, re-run extraction, export.
- Duplicate photos are detected (content hash) and not ingested twice.

**Extraction — per receipt**
- Store, date and time, subtotal, total, payment method.
- Line items: item code when printed, raw text as printed, quantity, unit of measure, unit price, percentage discount, line price.
- Quantity and unit price are optional; the printed line price is always required and is the source of truth.
- Normalised unit price (per lb, per oz, each) so pack sizes can be compared.
- Schema includes stubbed fields for v2: coupons, tax, fees.
- Non-grocery receipts are ingested like any other; nothing classifies or rejects them.

**Validation and review**
- Automatic checks flag a receipt, e.g. line items do not sum to the subtotal, missing date or store, low model confidence, implausible quantity or price.
- All flagged receipts go to a review queue in the web UI: photo next to the parsed items, editable.
- A **random sample of unflagged data** is also reviewed, in two tiers:
  - **Line items (main tier).** A random share of line items from unflagged receipts. The reviewer sees the receipt photo beside the one parsed line and confirms or corrects it. The receipt's store and date are checked at the same time.
  - **Whole receipts (calibration tier).** About 1 in 20 unflagged receipts is reviewed in full, to catch errors the line sample cannot see, such as a missed or invented line.
  - A correction in the line tier escalates that receipt to a full review.
- The sample rate is tracked separately per store and extractor backend, from a rolling window of recent sampled items. Thresholds are configurable starting values:
  - New store, new backend, or new model version: review everything for the first 10 receipts.
  - Error rate under 2%: sample 5% of line items.
  - 2–5%: sample 15%.
  - Over 5%: sample 50% and show a warning.
- Corrections are stored as such, so error rates can be measured per backend and per store.

**Product matching**
- Each line item is matched to a canonical product. The app suggests a match; a person confirms or corrects it.
- Matching uses the store's item code when the receipt prints one, and falls back to the raw text otherwise.
- Confirmed codes and text aliases are remembered per store, so the same line is matched automatically next time.
- Products have a category. Products, codes, and categories are seeded from the golden workbook's `Lookups` sheet.

**Users**
- No passwords. A name picker identifies the person.
- Uploads, reviews, and corrections are attributed to that person.
- Per-user settings exist from v1 so dashboards can be customised per person later.

**Output**
- CSV export of line items (with receipt, store, product, and normalised unit price).

**Photo retention**
- Originals are moved from the watched folder into a managed archive.
- Every photo is kept at least 1 year and deleted at 3 years.
- When the archive exceeds a size cap, the oldest photos beyond the 1-year minimum are deleted first. The 1-year minimum wins over the cap; the app warns if the cap cannot be met.
- A photo is exempt from deletion while its receipt is still waiting for review, including past 3 years.
- Extracted data is never deleted by retention.

### v2

- Product price history: chart and table per product, per store.
- Store comparison for a product or a basket.
- Receipt browser with search.
- JSON export.
- Per-user dashboard customisation.
- Coupons, tax, and fees captured per item.
- Additional extractor backends.

### v3 — mobile capture

- Android/iOS app: take a photo, send it straight to ingestion, never save it to the camera roll.
- Photos stored and backed up in Proton Drive, with the same retention rules managed automatically.

## 5. Golden data set

A workbook of receipts entered by hand before this project lives under `data/`, which git ignores. It is the reference for testing extraction accuracy and the seed for the product catalogue. Its profile (sizes, store breakdown, and cleanup list) is in `REQUIREMENTS.local.md`.

**What it implies for the design**
- **Item codes are the best matching key where a store prints them.** Matching uses the code when present and falls back to name aliases otherwise.
- **The receipt's line total is the source of truth.** Quantity × unit price can differ from the printed total by a few cents, because receipts round the weight. Validation needs a rounding tolerance and must not "correct" the printed total.
- **Percentage discounts appear on real receipts.** Line totals do not reconcile without them.
- **Quantity and unit price can be absent.** Some lines have only a line total.
- **Units need a controlled vocabulary.** The same unit is spelled several ways, and pack size often sits in the item name (e.g. "Onion 3lb") and must be parsed for normalised unit prices.
- **The same code can change price and name spelling.** Price belongs to the purchase, not the product.
- **Price history exists from day one**, because many products were bought more than once.
- **The workbook has no receipt photos and no receipt-level fields** (subtotal, total, tax, payment method), so extraction cannot be scored against it until matching photos are supplied.

**Import**
- A one-off CLI import loads the workbook into the database as reviewed, human-entered data, flagged with its origin. It cleans up known data-entry inconsistencies and reports what it changed.

## 6. Open questions

Tracked in the Linear issue named after each.

1. **Sampling parameters** — tune the starting thresholds once there is real data (AA-61).
2. **Archive size cap** — the value, and whether it is configurable in the UI (AA-65).
3. **Vision model** — which model that fits in the model host's GPU memory gives the best results on real receipts (AA-56).
4. **Backups** — how the SQLite database is backed up; the photos have a policy, the data does not yet (AA-67).
5. **Golden photos** — not available yet; the aim is to photograph all the golden receipts. Until then the workbook serves as catalogue seed and price history only, and extraction is checked by hand. Photos go in `data/golden/`, named `<date>_<store>_<receipt #>` (e.g. `2026-01-15_STORENAME_12345.jpg`, with `-1`, `-2` suffixes for multi-photo receipts) so each links to its rows without a lookup table (AA-56).

## 7. Backlog

Work is tracked in the Linear project **Receipt Tracker** (issues AA-49 to AA-75). Linear is canonical for scope and progress; this file is background. Seed: `docs/linear-backlog.md`.
