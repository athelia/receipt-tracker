"""One-off import of the hand-entered "golden" workbook.

The workbook has two sheets:

* a line-item sheet with one row per purchased item, and
* a lookup sheet mapping one store's item codes to product name, unit, and
  category.

Imported receipts are marked `origin = 'golden'` and count as reviewed. The
import is idempotent: a receipt already imported is skipped, and products,
codes, categories, and stores are reused.

Every row that needed cleaning up, or could not be imported, is listed in the
returned report.
"""

import datetime as dt
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from receipt_tracker.units import canonical_unit

ITEMS_SHEET = "Groceries Raw"
LOOKUP_SHEET = "Lookups"

# Header text (lower-cased) -> field name.
_ITEM_COLUMNS = {
    "date": "date",
    "store": "store",
    "receipt #": "receipt_number",
    "item code": "code",
    "item name": "name",
    "qty": "quantity",
    "cost basis": "unit_price",
    "mass unit": "unit",
    "discount": "discount",
    "cost (receipt)": "line_price",
    "item category": "category",
}
_LOOKUP_COLUMNS = {
    "code": "code",
    "name": "name",
    "unit": "unit",
    "category": "category",
}
_EXCEL_EPOCH = dt.date(1899, 12, 30)


class WorkbookError(ValueError):
    """The workbook does not have the expected sheets or columns."""


@dataclass
class ItemRow:
    row: int
    date: object
    store: object
    receipt_number: object
    code: object
    name: object
    quantity: object
    unit_price: object
    unit: object
    discount: object
    line_price: object
    category: object


@dataclass
class LookupRow:
    row: int
    code: object
    name: object
    unit: object
    category: object


@dataclass
class ImportReport:
    line_items_in_workbook: int = 0
    receipts_in_workbook: int = 0
    receipts_created: int = 0
    receipts_skipped_existing: int = 0
    line_items_created: int = 0
    line_items_matched: int = 0
    stores_created: int = 0
    categories_created: int = 0
    products_created: int = 0
    product_codes_created: int = 0
    lookup_store: str | None = None
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def note(self, sheet: str, row: int, message: str) -> None:
        self.notes.append(f"{sheet} row {row}: {message}")

    def lines(self) -> list[str]:
        out = [
            f"Workbook: {self.line_items_in_workbook} line items in "
            f"{self.receipts_in_workbook} receipts",
            f"Receipts: {self.receipts_created} created, "
            f"{self.receipts_skipped_existing} already imported",
            f"Line items: {self.line_items_created} created, "
            f"{self.line_items_matched} matched to a product by item code",
            f"Created: {self.stores_created} stores, {self.categories_created} categories, "
            f"{self.products_created} products, {self.product_codes_created} item codes",
            f"Lookup sheet applied to store: {self.lookup_store or '(none)'}",
        ]
        if self.notes:
            out.append(f"Cleaned up or could not map ({len(self.notes)}):")
            out.extend(f"  {note}" for note in self.notes)
        if self.dry_run:
            out.append("Dry run: nothing was saved.")
        return out


# --- reading ----------------------------------------------------------------


def read_workbook(path: Path | str) -> tuple[list[ItemRow], list[LookupRow]]:
    """Read both sheets. Formula cells yield the values Excel last calculated."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        items = [ItemRow(**r) for r in _read_sheet(workbook, ITEMS_SHEET, _ITEM_COLUMNS)]
        lookups = [LookupRow(**r) for r in _read_sheet(workbook, LOOKUP_SHEET, _LOOKUP_COLUMNS)]
    finally:
        workbook.close()
    return items, lookups


def _read_sheet(workbook, sheet_name: str, columns: dict[str, str]) -> list[dict]:
    if sheet_name not in workbook.sheetnames:
        raise WorkbookError(f"Sheet {sheet_name!r} not found (has: {workbook.sheetnames})")
    rows = workbook[sheet_name].iter_rows(values_only=True)
    header = next(rows, None) or ()
    positions = {}
    for index, cell in enumerate(header):
        key = str(cell).strip().lower() if cell is not None else ""
        if key in columns and columns[key] not in positions:
            positions[columns[key]] = index
    missing = sorted(set(columns.values()) - set(positions))
    if missing:
        raise WorkbookError(f"Sheet {sheet_name!r} is missing columns for: {', '.join(missing)}")
    out = []
    for number, values in enumerate(rows, start=2):
        record = {
            name: (values[index] if index < len(values) else None)
            for name, index in positions.items()
        }
        record["row"] = number
        out.append(record)
    return out


# --- value cleaning -----------------------------------------------------------


def _text(value: object) -> str | None:
    """Trimmed text, or None for empty cells and Excel errors such as #N/A."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    return text


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # repr() gives the shortest text that round-trips, so 18.989999999999998
        # (what Excel stores for 18.99) becomes Decimal("18.99").
        return Decimal(repr(round(value, 6)))
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


def _scaled(value: object, factor: int) -> tuple[int | None, bool]:
    """Scale to an integer. Returns (result, exact)."""
    number = _decimal(value)
    if number is None:
        return None, True
    scaled = number * factor
    rounded = scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(rounded), rounded == scaled


def _code(value: object) -> str | None:
    """Item codes as text; numeric cells lose the trailing `.0`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def _date(value: object) -> str | None:
    """ISO date from a date cell, an Excel serial number, or ISO text."""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (_EXCEL_EPOCH + dt.timedelta(days=int(value))).isoformat()
    text = _text(value)
    if text is None:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _store_key(name: str) -> str:
    """Spellings that differ only in case, spacing, or punctuation share a key."""
    return re.sub(r"[^a-z0-9]", "", name.casefold())


# --- import -------------------------------------------------------------------


def import_golden(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    lookup_store: str | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import the workbook at `path`. All changes are made in one transaction.

    `lookup_store` names the store whose item codes the lookup sheet describes.
    When omitted it is inferred as the store whose rows match the most lookup
    codes.
    """
    items, lookups = read_workbook(path)
    report = ImportReport(dry_run=dry_run)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _import(conn, items, lookups, lookup_store, report)
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def _import(conn, items, lookups, lookup_store_name, report) -> None:
    rows = _usable_rows(items, report)
    store_names = _canonical_stores(rows, report)
    lookup_by_code = _lookup_by_code(lookups, report)

    lookup_store = _choose_lookup_store(rows, store_names, lookup_by_code, lookup_store_name)
    report.lookup_store = lookup_store

    store_ids = {}
    for name in dict.fromkeys(store_names.values()):
        store_ids[name] = _get_or_create(conn, "stores", report, "stores_created", name=name)

    products_by_code = {}
    if lookup_store is not None:
        products_by_code = _seed_products(conn, store_ids[lookup_store], lookup_by_code, report)

    receipts = defaultdict(list)
    for row in rows:
        store = store_names[row.row]
        receipts[(_date(row.date), store, _code(row.receipt_number))].append(row)
    report.line_items_in_workbook = len(rows)
    report.receipts_in_workbook = len(receipts)

    for (date, store, number), receipt_rows in receipts.items():
        store_id = store_ids[store]
        existing = conn.execute(
            "SELECT id FROM receipts WHERE origin = 'golden' AND store_id = ? "
            "AND purchased_at IS ? AND receipt_number IS ?",
            (store_id, date, number),
        ).fetchone()
        if existing:
            report.receipts_skipped_existing += 1
            continue
        receipt_id = conn.execute(
            "INSERT INTO receipts (store_id, purchased_at, receipt_number, origin, review_status) "
            "VALUES (?, ?, ?, 'golden', 'accepted')",
            (store_id, date, number),
        ).lastrowid
        report.receipts_created += 1
        codes = products_by_code if store == lookup_store else {}
        for position, row in enumerate(receipt_rows, start=1):
            _insert_line_item(conn, receipt_id, position, row, codes, store == lookup_store, report)


def _usable_rows(items: list[ItemRow], report: ImportReport) -> list[ItemRow]:
    """Drop blank rows silently; report rows that have data but cannot be placed."""
    usable = []
    for row in items:
        has_date = _date(row.date) is not None
        has_store = _text(row.store) is not None
        if not has_date and not has_store and _code(row.code) is None:
            continue  # an empty table row (often formula-only)
        if not has_date or not has_store:
            report.note(ITEMS_SHEET, row.row, "skipped: missing date or store")
            continue
        if _decimal(row.line_price) is None:
            report.note(ITEMS_SHEET, row.row, "skipped: no receipt price")
            continue
        usable.append(row)
    return usable


def _canonical_stores(rows: list[ItemRow], report: ImportReport) -> dict[int, str]:
    """Map each row number to its store name, merging near-identical spellings."""
    spellings: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        name = _text(row.store)
        spellings[_store_key(name)][name] += 1
    canonical = {key: counts.most_common(1)[0][0] for key, counts in spellings.items()}
    out = {}
    for row in rows:
        name = _text(row.store)
        chosen = canonical[_store_key(name)]
        if name != chosen:
            report.note(ITEMS_SHEET, row.row, f"store {name!r} merged into {chosen!r}")
        out[row.row] = chosen
    return out


def _lookup_by_code(lookups: list[LookupRow], report: ImportReport) -> dict[str, LookupRow]:
    by_code: dict[str, LookupRow] = {}
    for row in lookups:
        code, name = _code(row.code), _text(row.name)
        if code is None and name is None:
            continue
        if code is None or name is None:
            report.note(LOOKUP_SHEET, row.row, "skipped: missing code or name")
            continue
        if code in by_code:
            first = by_code[code]
            report.note(
                LOOKUP_SHEET,
                row.row,
                f"code {code} repeats row {first.row}; kept {_text(first.name)!r}",
            )
            continue
        by_code[code] = row
    return by_code


def _choose_lookup_store(rows, store_names, lookup_by_code, requested) -> str | None:
    known = set(store_names.values())
    if requested is not None:
        match = next((n for n in known if _store_key(n) == _store_key(requested)), None)
        if match is None:
            raise WorkbookError(f"Store {requested!r} not found (has: {sorted(known)})")
        return match
    hits = Counter(store_names[row.row] for row in rows if _code(row.code) in lookup_by_code)
    return hits.most_common(1)[0][0] if hits else None


def _seed_products(conn, store_id, lookup_by_code, report) -> dict[str, int]:
    """Create products, categories, and this store's item codes from the lookup sheet."""
    products = {}
    for code, row in lookup_by_code.items():
        existing = conn.execute(
            "SELECT product_id FROM product_codes WHERE store_id = ? AND code = ?",
            (store_id, code),
        ).fetchone()
        if existing:
            products[code] = existing["product_id"]
            continue
        category_id = None
        category = _text(row.category)
        if category is not None:
            category_id = _get_or_create(
                conn, "categories", report, "categories_created", name=category
            )
        product_id = conn.execute(
            "INSERT INTO products (name, category_id, default_unit) VALUES (?, ?, ?)",
            (_text(row.name), category_id, canonical_unit(_text(row.unit))),
        ).lastrowid
        report.products_created += 1
        conn.execute(
            "INSERT INTO product_codes (store_id, code, product_id) VALUES (?, ?, ?)",
            (store_id, code, product_id),
        )
        report.product_codes_created += 1
        products[code] = product_id
    return products


def _insert_line_item(conn, receipt_id, position, row, products_by_code, has_lookup, report):
    code, name = _code(row.code), _text(row.name)
    if name is None and code is not None and not code.isdigit():
        # The description was typed into the code column.
        report.note(ITEMS_SHEET, row.row, "item name found in the code column; no item code")
        name, code = code, None
    if name is None:
        name = f"(unnamed item {code})" if code else "(unnamed item)"
        report.note(ITEMS_SHEET, row.row, "no item name")

    product_id = products_by_code.get(code) if code else None
    if has_lookup and code and product_id is None:
        report.note(ITEMS_SHEET, row.row, f"item code {code} is not in the lookup sheet")

    raw_unit = _text(row.unit)
    unit = canonical_unit(raw_unit)
    if raw_unit is not None and unit != raw_unit:
        report.note(ITEMS_SHEET, row.row, f"unit {raw_unit!r} stored as {unit!r}")

    quantity, quantity_exact = _scaled(row.quantity, 1000)
    unit_price, unit_price_exact = _scaled(row.unit_price, 100)
    discount, discount_exact = _scaled(row.discount, 10000)
    line_price, line_price_exact = _scaled(row.line_price, 100)
    for label, exact in (
        ("quantity", quantity_exact),
        ("unit price", unit_price_exact),
        ("discount", discount_exact),
        ("receipt price", line_price_exact),
    ):
        if not exact:
            report.note(ITEMS_SHEET, row.row, f"{label} rounded to fit storage precision")
    if discount is not None and not 0 <= discount <= 10000:
        report.note(ITEMS_SHEET, row.row, "discount outside 0-100% dropped")
        discount = None
    if discount == 0:
        discount = None

    conn.execute(
        "INSERT INTO line_items (receipt_id, position, item_code, raw_text, quantity_milli, unit, "
        "unit_price_cents, discount_bp, line_price_cents, product_id, product_match) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            receipt_id,
            position,
            code,
            name,
            quantity,
            unit,
            unit_price,
            discount,
            line_price,
            product_id,
            "confirmed" if product_id is not None else "none",
        ),
    )
    report.line_items_created += 1
    if product_id is not None:
        report.line_items_matched += 1


def _get_or_create(conn, table: str, report: ImportReport, counter: str, *, name: str) -> int:
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    setattr(report, counter, getattr(report, counter) + 1)
    return conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid
