import datetime as dt

import pytest
from openpyxl import Workbook
from typer.testing import CliRunner

from receipt_tracker import db
from receipt_tracker.cli import app
from receipt_tracker.config import get_settings
from receipt_tracker.golden_import import WorkbookError, import_golden
from receipt_tracker.units import canonical_unit

ITEM_HEADER = [
    "Date",
    "Store",
    "Receipt #",
    "Item Code",
    "Item Name",
    "Qty",
    "Cost Basis",
    "Mass Unit",
    "Discount",
    "Cost (calc)",
    "Cost (Receipt)",
    "Error",
    "Item Category",
]
LOOKUP_HEADER = ["Code", "Name", "Typical PPU", "Unit", "Category"]
DAY = dt.datetime(2026, 1, 15)


def item(
    date=DAY,
    store="Corner Market",
    receipt="1001",
    code=None,
    name=None,
    qty=None,
    unit_price=None,
    unit=None,
    discount=None,
    price=1.00,
    category=None,
):
    return [date, store, receipt, code, name, qty, unit_price, unit, discount, None, price, 0]


LOOKUPS = [
    [4011, "Bananas", 0.59, "lb", "Fruit"],
    [4850, "Aged Cheddar", None, "lbs", "Cheese"],
    [70001, "Sparkling Water", 1.29, "ea", None],
]


def make_workbook(tmp_path, items, lookups=LOOKUPS, item_header=ITEM_HEADER, sheets=None):
    workbook = Workbook()
    raw = workbook.active
    raw.title = (sheets or ["Groceries Raw", "Lookups"])[0]
    raw.append(item_header)
    for row in items:
        raw.append(row)
    lookup_sheet = workbook.create_sheet((sheets or ["Groceries Raw", "Lookups"])[1])
    lookup_sheet.append(LOOKUP_HEADER)
    for row in lookups:
        lookup_sheet.append(row)
    path = tmp_path / "golden.xlsx"
    workbook.save(path)
    return path


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def line_items(conn):
    return conn.execute("SELECT * FROM line_items ORDER BY receipt_id, position").fetchall()


# --- receipts and counts ------------------------------------------------------


def test_rows_are_grouped_into_receipts_by_date_store_and_number(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(receipt="1001", code=4011, name="Bananas", price=1.55),
            item(receipt="1001", code=70001, name="Sparkling Water", price=1.29),
            item(receipt="1002", code=4011, name="Bananas", price=0.80),
            item(date=dt.datetime(2026, 1, 16), receipt="1001", code=4011, name="Bananas"),
            item(store="Other Market", receipt="1001", name="Oat Milk", price=3.99),
        ],
    )
    report = import_golden(conn, path)

    assert (report.line_items_in_workbook, report.receipts_in_workbook) == (5, 4)
    assert (report.line_items_created, report.receipts_created) == (5, 4)
    assert (count(conn, "line_items"), count(conn, "receipts")) == (5, 4)
    positions = conn.execute(
        "SELECT position FROM line_items WHERE receipt_id = 1 ORDER BY position"
    ).fetchall()
    assert [p[0] for p in positions] == [1, 2]


def test_imported_receipts_are_golden_and_already_reviewed(conn, tmp_path):
    import_golden(conn, make_workbook(tmp_path, [item(name="Oat Milk")]))
    row = conn.execute("SELECT * FROM receipts").fetchone()
    assert (row["origin"], row["review_status"]) == ("golden", "accepted")
    assert (row["purchased_at"], row["receipt_number"]) == ("2026-01-15", "1001")
    assert row["total_cents"] is None


def test_importing_twice_creates_no_duplicates(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [item(code=4011, name="Bananas"), item(store="Other Market", name="Oat Milk")],
    )
    import_golden(conn, path)
    before = {t: count(conn, t) for t in ("receipts", "line_items", "products", "stores")}

    second = import_golden(conn, path)

    assert {t: count(conn, t) for t in before} == before
    assert (second.receipts_created, second.receipts_skipped_existing) == (0, 2)
    assert (second.products_created, second.stores_created) == (0, 0)


def test_receipt_without_a_number_is_imported_and_stays_idempotent(conn, tmp_path):
    path = make_workbook(tmp_path, [item(receipt=None, name="Oat Milk")])
    import_golden(conn, path)
    import_golden(conn, path)
    assert count(conn, "receipts") == 1
    assert conn.execute("SELECT receipt_number FROM receipts").fetchone()[0] is None


def test_dry_run_saves_nothing(conn, tmp_path):
    path = make_workbook(tmp_path, [item(code=4011, name="Bananas")])
    report = import_golden(conn, path, dry_run=True)
    assert report.receipts_created == 1
    assert "Dry run: nothing was saved." in report.lines()
    assert all(count(conn, t) == 0 for t in ("receipts", "line_items", "products", "stores"))


def test_blank_rows_are_ignored_and_incomplete_rows_reported(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(name="Oat Milk"),
            [None, None, None, None, "#N/A", None, None, "#N/A", None, 0, None, 0],
            item(store=None, name="Mystery"),
            item(name="No Price", price=None),
        ],
    )
    report = import_golden(conn, path)
    assert report.line_items_in_workbook == 1
    assert report.notes == [
        "Groceries Raw row 4: skipped: missing date or store",
        "Groceries Raw row 5: skipped: no receipt price",
    ]


# --- stores -----------------------------------------------------------------


def test_near_identical_store_spellings_are_merged_and_reported(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(receipt="1", name="A"),
            item(receipt="2", name="B"),
            item(receipt="3", store="Corner Market`", name="C"),
            item(receipt="4", store="corner  market", name="D"),
            item(receipt="5", store="Corner Market Pharmacy", name="E"),
        ],
    )
    report = import_golden(conn, path)
    names = [r[0] for r in conn.execute("SELECT name FROM stores ORDER BY name")]
    assert names == ["Corner Market", "Corner Market Pharmacy"]
    assert report.notes == [
        "Groceries Raw row 4: store 'Corner Market`' merged into 'Corner Market'",
        "Groceries Raw row 5: store 'corner  market' merged into 'Corner Market'",
    ]


# --- products and matching ----------------------------------------------------


def test_lookup_sheet_seeds_products_categories_and_codes(conn, tmp_path):
    report = import_golden(conn, make_workbook(tmp_path, [item(code=4011, name="Bananas")]))

    assert report.lookup_store == "Corner Market"
    assert (report.products_created, report.product_codes_created) == (3, 3)
    assert report.categories_created == 2
    rows = conn.execute(
        "SELECT p.name, p.default_unit, c.name AS category, pc.code, s.name AS store "
        "FROM products p JOIN product_codes pc ON pc.product_id = p.id "
        "JOIN stores s ON s.id = pc.store_id LEFT JOIN categories c ON c.id = p.category_id "
        "ORDER BY pc.code"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("Bananas", "lb", "Fruit", "4011", "Corner Market"),
        ("Aged Cheddar", "lb", "Cheese", "4850", "Corner Market"),
        ("Sparkling Water", "ea", None, "70001", "Corner Market"),
    ]


def test_line_items_are_matched_by_code_only_in_the_lookup_store(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(receipt="1", code=4011, name="Bananas"),
            item(receipt="2", code=4850, name="Aged Cheddar"),
            item(receipt="3", store="Other Market", code=4011, name="Plantain"),
        ],
    )
    report = import_golden(conn, path)
    rows = conn.execute(
        "SELECT li.item_code, li.raw_text, li.product_match, p.name AS product "
        "FROM line_items li LEFT JOIN products p ON p.id = li.product_id ORDER BY li.id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("4011", "Bananas", "confirmed", "Bananas"),
        ("4850", "Aged Cheddar", "confirmed", "Aged Cheddar"),
        ("4011", "Plantain", "none", None),
    ]
    assert report.line_items_matched == 2
    assert report.notes == []


def test_lookup_store_can_be_named_explicitly(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(receipt="1", code=4011, name="Bananas"),
            item(receipt="2", code=4850, name="Aged Cheddar"),
            item(receipt="3", store="Other Market", code=4011, name="Bananas"),
        ],
    )
    report = import_golden(conn, path, lookup_store="other market")
    assert report.lookup_store == "Other Market"
    matched = conn.execute(
        "SELECT s.name FROM line_items li JOIN receipts r ON r.id = li.receipt_id "
        "JOIN stores s ON s.id = r.store_id WHERE li.product_id IS NOT NULL"
    ).fetchall()
    assert [m[0] for m in matched] == ["Other Market"]


def test_unknown_lookup_store_is_an_error_and_saves_nothing(conn, tmp_path):
    path = make_workbook(tmp_path, [item(code=4011, name="Bananas")])
    with pytest.raises(WorkbookError, match="Nowhere"):
        import_golden(conn, path, lookup_store="Nowhere")
    assert count(conn, "receipts") == 0


def test_code_missing_from_lookup_is_imported_unmatched_and_reported(conn, tmp_path):
    path = make_workbook(
        tmp_path, [item(code=4011, name="Bananas"), item(code=9999, name="Mystery Fruit")]
    )
    report = import_golden(conn, path)
    mystery = line_items(conn)[1]
    assert (mystery["item_code"], mystery["product_id"]) == ("9999", None)
    assert report.notes == ["Groceries Raw row 3: item code 9999 is not in the lookup sheet"]


def test_repeated_lookup_code_keeps_the_first_row(conn, tmp_path):
    lookups = [*LOOKUPS, [4011, "Bananas Organic", 0.79, "lb", "Fruit"]]
    path = make_workbook(tmp_path, [item(code=4011, name="Bananas")], lookups=lookups)
    report = import_golden(conn, path)
    assert count(conn, "products") == 3
    assert report.notes == ["Lookups row 5: code 4011 repeats row 2; kept 'Bananas'"]


def test_item_name_typed_into_the_code_column(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(code=4011, name="Bananas"),
            item(store="Other Market", receipt="2", code="Whole Milk", name="#N/A"),
        ],
    )
    report = import_golden(conn, path)
    milk = line_items(conn)[1]
    assert (milk["raw_text"], milk["item_code"]) == ("Whole Milk", None)
    assert report.notes == ["Groceries Raw row 3: item name found in the code column; no item code"]


# --- values -----------------------------------------------------------------


def test_amounts_are_stored_exactly_despite_spreadsheet_float_noise(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(
                code=4850.0,
                name="Aged Cheddar",
                qty=0.57999999999999996,
                unit_price=18.989999999999998,
                unit="lb",
                discount=0.2,
                price=8.8100000000000005,
            )
        ],
    )
    report = import_golden(conn, path)
    row = line_items(conn)[0]
    assert row["item_code"] == "4850"
    assert (row["quantity_milli"], row["unit_price_cents"]) == (580, 1899)
    assert (row["discount_bp"], row["line_price_cents"]) == (2000, 881)
    assert report.notes == []


def test_line_with_only_a_price_is_stored(conn, tmp_path):
    import_golden(
        conn, make_workbook(tmp_path, [item(name="Dried Noodles", unit="#N/A", price=4.99)])
    )
    row = line_items(conn)[0]
    assert row["line_price_cents"] == 499
    assert (row["quantity_milli"], row["unit"], row["unit_price_cents"]) == (None, None, None)
    assert row["discount_bp"] is None


def test_unit_spellings_are_folded_and_reported(conn, tmp_path):
    path = make_workbook(
        tmp_path,
        [
            item(name="Kale", unit="bch"),
            item(name="Flour", unit="lbs"),
            item(name="Tea", unit="ea"),
        ],
    )
    report = import_golden(conn, path)
    assert [r["unit"] for r in line_items(conn)] == ["bunch", "lb", "ea"]
    assert report.notes == [
        "Groceries Raw row 2: unit 'bch' stored as 'bunch'",
        "Groceries Raw row 3: unit 'lbs' stored as 'lb'",
    ]


def test_values_finer_than_storage_precision_are_rounded_and_reported(conn, tmp_path):
    path = make_workbook(tmp_path, [item(name="Saffron", qty=0.12345, price=2.50)])
    report = import_golden(conn, path)
    assert line_items(conn)[0]["quantity_milli"] == 123
    assert report.notes == ["Groceries Raw row 2: quantity rounded to fit storage precision"]


@pytest.mark.parametrize(
    "cell", [dt.datetime(2026, 1, 15, 9, 30), dt.date(2026, 1, 15), 46037, "2026-01-15"]
)
def test_dates_are_read_from_date_cells_serial_numbers_and_text(conn, tmp_path, cell):
    import_golden(conn, make_workbook(tmp_path, [item(date=cell, name="Oat Milk")]))
    assert conn.execute("SELECT purchased_at FROM receipts").fetchone()[0] == "2026-01-15"


# --- workbook shape -----------------------------------------------------------


def test_missing_sheet_is_reported(conn, tmp_path):
    path = make_workbook(tmp_path, [item(name="A")], sheets=["Sheet1", "Lookups"])
    with pytest.raises(WorkbookError, match="Groceries Raw"):
        import_golden(conn, path)


def test_missing_column_is_reported(conn, tmp_path):
    header = [h for h in ITEM_HEADER if h != "Cost (Receipt)"]
    path = make_workbook(tmp_path, [], item_header=header)
    with pytest.raises(WorkbookError, match="line_price"):
        import_golden(conn, path)


def test_columns_are_found_by_header_not_position(conn, tmp_path):
    header = list(reversed(ITEM_HEADER))
    row = list(reversed([*item(code=4011, name="Bananas", price=1.55), None]))
    path = make_workbook(tmp_path, [row], item_header=header)
    import_golden(conn, path)
    assert line_items(conn)[0]["line_price_cents"] == 155


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "store"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_cli_imports_and_prints_the_report(cli_env):
    path = make_workbook(cli_env, [item(code=4011, name="Bananas"), item(code=9999, name="X")])
    result = CliRunner().invoke(app, ["import-golden", str(path)])
    assert result.exit_code == 0
    assert "Workbook: 2 line items in 1 receipts" in result.output
    assert "Receipts: 1 created, 0 already imported" in result.output
    assert "row 3: item code 9999 is not in the lookup sheet" in result.output

    connection = db.connect(cli_env / "store" / "receipt_tracker.db")
    assert count(connection, "line_items") == 2
    connection.close()


def test_cli_reports_an_unusable_workbook(cli_env):
    path = make_workbook(cli_env, [], sheets=["Sheet1", "Lookups"])
    result = CliRunner().invoke(app, ["import-golden", str(path)])
    assert result.exit_code == 1
    assert "Cannot import" in result.output


# --- units ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("lb", "lb"), ("LBS", "lb"), (" bch ", "bunch"), ("HD", "hd"), ("", None), (None, None)],
)
def test_canonical_unit(raw, expected):
    assert canonical_unit(raw) == expected
