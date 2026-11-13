import sqlite3

import pytest
from typer.testing import CliRunner

from receipt_tracker import db
from receipt_tracker.cli import app
from receipt_tracker.config import get_settings

EXPECTED_TABLES = {
    "categories",
    "corrections",
    "extractions",
    "jobs",
    "line_items",
    "photos",
    "product_aliases",
    "product_codes",
    "products",
    "receipts",
    "schema_migrations",
    "stores",
    "user_settings",
    "users",
}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def tables(connection):
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def insert(connection, table, **values):
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )
    return cursor.lastrowid


@pytest.fixture
def receipt(conn):
    store = insert(conn, "stores", name="Corner Market")
    return insert(conn, "receipts", store_id=store, origin="extracted")


# --- migrations ---------------------------------------------------------


def test_migrate_creates_every_table(conn):
    assert tables(conn) == EXPECTED_TABLES


def test_migrate_records_what_it_applied(conn):
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(r["version"], r["name"]) for r in rows] == [(1, "initial")]


def test_migrate_twice_applies_nothing_new(conn):
    assert db.migrate(conn) == []


def test_failed_migration_leaves_database_unchanged():
    connection = db.connect(":memory:")
    broken = db.Migration(
        1,
        "broken",
        "CREATE TABLE kept_out (id INTEGER PRIMARY KEY);\nCREATE TABLE oops (;\n",
    )
    with pytest.raises(sqlite3.Error):
        db.migrate(connection, [broken])
    assert tables(connection) == {"schema_migrations"}
    assert db.applied_versions(connection) == set()


def test_later_migrations_apply_on_top_of_earlier_ones():
    connection = db.connect(":memory:")
    first = db.Migration(1, "first", "CREATE TABLE a (id INTEGER PRIMARY KEY);")
    second = db.Migration(2, "second", "CREATE TABLE b (id INTEGER PRIMARY KEY);")
    assert db.migrate(connection, [first]) == [first]
    assert db.migrate(connection, [second, first]) == [second]
    assert {"a", "b"} <= tables(connection)


def test_statement_splitting_ignores_semicolons_in_strings_and_comments():
    sql = (
        "-- leading comment; with a semicolon\n"
        "CREATE TABLE t (note TEXT DEFAULT 'a;b');\n"
        "INSERT INTO t DEFAULT VALUES;\n"
        "-- trailing comment\n"
    )
    connection = db.connect(":memory:")
    db.migrate(connection, [db.Migration(1, "split", sql)])
    assert connection.execute("SELECT note FROM t").fetchone()["note"] == "a;b"


def test_incomplete_final_statement_is_rejected():
    connection = db.connect(":memory:")
    with pytest.raises(ValueError, match="incomplete"):
        db.migrate(connection, [db.Migration(1, "cut_off", "CREATE TABLE t (id INTEGER)")])
    assert "t" not in tables(connection)


def test_connect_creates_missing_folder_and_persists(tmp_path):
    path = tmp_path / "nested" / "store.db"
    connection = db.connect(path)
    db.migrate(connection)
    insert(connection, "stores", name="Corner Market")
    connection.close()

    reopened = db.connect(path)
    assert reopened.execute("SELECT name FROM stores").fetchone()["name"] == "Corner Market"
    assert db.migrate(reopened) == []
    reopened.close()


def test_cli_migrate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "store"))
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        first = runner.invoke(app, ["migrate"])
        second = runner.invoke(app, ["migrate"])
    finally:
        get_settings.cache_clear()
    assert first.exit_code == 0
    assert "applied 0001 initial" in first.output
    assert second.exit_code == 0
    assert "up to date" in second.output
    assert (tmp_path / "store" / "receipt_tracker.db").exists()


# --- receipts and line items --------------------------------------------


def test_line_item_with_only_a_line_price_can_be_stored(conn, receipt):
    insert(
        conn,
        "line_items",
        receipt_id=receipt,
        position=1,
        raw_text="DRIED NOODLES",
        line_price_cents=499,
    )
    row = conn.execute("SELECT * FROM line_items").fetchone()
    assert row["line_price_cents"] == 499
    assert row["quantity_milli"] is None
    assert row["unit"] is None
    assert row["unit_price_cents"] is None
    assert row["product_match"] == "none"


def test_full_line_item_round_trips(conn, receipt):
    insert(
        conn,
        "line_items",
        receipt_id=receipt,
        position=1,
        item_code="4011",
        raw_text="BANANAS",
        quantity_milli=2930,
        unit="lb",
        unit_price_cents=53,
        discount_bp=2000,
        line_price_cents=124,
    )
    row = conn.execute("SELECT * FROM line_items").fetchone()
    assert (row["quantity_milli"], row["unit_price_cents"], row["discount_bp"]) == (2930, 53, 2000)


def test_line_price_is_required(conn, receipt):
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "line_items", receipt_id=receipt, position=1, raw_text="BANANAS")


def test_money_columns_reject_fractional_values(conn, receipt):
    with pytest.raises(sqlite3.IntegrityError):
        insert(
            conn,
            "line_items",
            receipt_id=receipt,
            position=1,
            raw_text="BANANAS",
            line_price_cents=1.55,
        )


def test_discount_must_be_between_zero_and_one_hundred_percent(conn, receipt):
    with pytest.raises(sqlite3.IntegrityError):
        insert(
            conn,
            "line_items",
            receipt_id=receipt,
            position=1,
            raw_text="BANANAS",
            line_price_cents=100,
            discount_bp=10001,
        )


def test_line_item_needs_an_existing_receipt(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "line_items", receipt_id=999, position=1, raw_text="X", line_price_cents=1)


def test_receipt_can_exist_before_anything_is_extracted(conn):
    receipt_id = insert(conn, "receipts", origin="extracted")
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    assert row["store_id"] is None
    assert row["review_status"] == "unreviewed"
    assert row["created_at"].endswith("Z")


@pytest.mark.parametrize(("column", "value"), [("origin", "guessed"), ("review_status", "maybe")])
def test_receipt_rejects_unknown_states(conn, column, value):
    values = {"origin": "golden", column: value}
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "receipts", **values)


def test_deleting_a_receipt_removes_its_line_items(conn, receipt):
    insert(conn, "line_items", receipt_id=receipt, position=1, raw_text="X", line_price_cents=1)
    conn.execute("DELETE FROM receipts WHERE id = ?", (receipt,))
    assert conn.execute("SELECT count(*) FROM line_items").fetchone()[0] == 0


# --- extractions and jobs -----------------------------------------------


def test_extraction_records_backend_and_model_version(conn, receipt):
    insert(
        conn,
        "extractions",
        receipt_id=receipt,
        backend="ollama-vision",
        model="example-vl",
        model_version="7b-q4",
        raw_output="{}",
    )
    row = conn.execute("SELECT backend, model, model_version FROM extractions").fetchone()
    assert tuple(row) == ("ollama-vision", "example-vl", "7b-q4")


@pytest.mark.parametrize("missing", ["backend", "model", "model_version"])
def test_extraction_requires_provenance(conn, receipt, missing):
    values = {"backend": "ollama-vision", "model": "example-vl", "model_version": "7b-q4"}
    del values[missing]
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "extractions", receipt_id=receipt, **values)


def test_extraction_rejects_empty_provenance(conn, receipt):
    with pytest.raises(sqlite3.IntegrityError):
        insert(
            conn,
            "extractions",
            receipt_id=receipt,
            backend="ollama-vision",
            model="example-vl",
            model_version="",
        )


def test_job_starts_queued_and_rejects_unknown_states(conn, receipt):
    job = insert(conn, "jobs", receipt_id=receipt)
    row = conn.execute("SELECT state, attempts FROM jobs WHERE id = ?", (job,)).fetchone()
    assert tuple(row) == ("queued", 0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET state = 'lost' WHERE id = ?", (job,))


# --- products -------------------------------------------------------------


def test_item_codes_are_unique_per_store_only(conn):
    first = insert(conn, "stores", name="Corner Market")
    second = insert(conn, "stores", name="Other Market")
    bananas = insert(conn, "products", name="Bananas")
    plantains = insert(conn, "products", name="Plantains")
    insert(conn, "product_codes", store_id=first, code="4011", product_id=bananas)
    insert(conn, "product_codes", store_id=second, code="4011", product_id=plantains)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "product_codes", store_id=first, code="4011", product_id=plantains)


def test_aliases_are_unique_per_store(conn):
    store = insert(conn, "stores", name="Corner Market")
    product = insert(conn, "products", name="Bananas")
    insert(conn, "product_aliases", store_id=store, raw_text="ORG BNNS", product_id=product)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "product_aliases", store_id=store, raw_text="ORG BNNS", product_id=product)


def test_deleting_a_product_unlinks_its_line_items(conn, receipt):
    product = insert(conn, "products", name="Bananas")
    insert(
        conn,
        "line_items",
        receipt_id=receipt,
        position=1,
        raw_text="BANANAS",
        line_price_cents=124,
        product_id=product,
    )
    conn.execute("DELETE FROM products WHERE id = ?", (product,))
    assert conn.execute("SELECT product_id FROM line_items").fetchone()["product_id"] is None


# --- users, photos, corrections -------------------------------------------


def test_user_settings_are_separate_per_user(conn):
    alex = insert(conn, "users", name="Alex")
    sam = insert(conn, "users", name="Sam")
    insert(conn, "user_settings", user_id=alex, key="dashboard", value='{"layout": "compact"}')
    insert(conn, "user_settings", user_id=sam, key="dashboard", value='{"layout": "wide"}')
    rows = conn.execute("SELECT value FROM user_settings WHERE user_id = ?", (alex,)).fetchall()
    assert [r["value"] for r in rows] == ['{"layout": "compact"}']


def test_duplicate_photo_content_is_rejected(conn, receipt):
    photo = {
        "receipt_id": receipt,
        "sha256": "ab" * 32,
        "original_filename": "IMG_0001.jpg",
        "archive_path": "2026/01/abab.jpg",
        "byte_size": 12345,
    }
    insert(conn, "photos", **photo)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "photos", **{**photo, "original_filename": "copy.jpg"})


def test_correction_survives_deletion_of_its_user(conn, receipt):
    user = insert(conn, "users", name="Alex")
    insert(
        conn,
        "corrections",
        receipt_id=receipt,
        field="total_cents",
        old_value="1000",
        new_value="1010",
        user_id=user,
    )
    conn.execute("DELETE FROM users WHERE id = ?", (user,))
    row = conn.execute("SELECT field, user_id FROM corrections").fetchone()
    assert tuple(row) == ("total_cents", None)
