import sqlite3

import pytest

from receipt_tracker import db, extraction_store, queue
from receipt_tracker.config import Settings
from receipt_tracker.extractors import (
    ExtractedHeader,
    ExtractedLine,
    ExtractedReceipt,
    ExtractionResult,
    available,
    create,
    register,
)
from receipt_tracker.extractors.fake import FakeExtractor


def test_fake_backend_is_registered_and_selectable_by_name():
    assert "fake" in available()
    result = create("fake", Settings()).extract([])
    assert (result.backend, result.model) == ("fake", "fake-model")


def test_unknown_backend_names_the_available_ones():
    with pytest.raises(ValueError, match="Available:.*fake"):
        create("nope", Settings())


def test_new_backend_registers_without_touching_other_code():
    register("test-lan", lambda settings: FakeExtractor(), image_destination="local_network")
    assert isinstance(create("test-lan", Settings()), FakeExtractor)


def test_backend_that_sends_images_elsewhere_is_refused():
    with pytest.raises(ValueError, match="home network"):
        register("cloud", lambda settings: FakeExtractor(), image_destination="cloud")  # type: ignore[arg-type]
    assert "cloud" not in available()


def test_duplicate_backend_name_is_refused():
    with pytest.raises(ValueError, match="already registered"):
        register("fake", lambda settings: FakeExtractor(), image_destination="none")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line_price_cents": 1.5},
        {"line_price_cents": True},
        {"line_price_cents": 1, "quantity_milli": 0.5},
        {"line_price_cents": 1, "discount_bp": 10001},
        {"line_price_cents": 1, "confidence": 1.5},
    ],
)
def test_line_validation_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        ExtractedLine("X", **kwargs)


def test_result_requires_provenance():
    with pytest.raises(ValueError):
        ExtractionResult(ExtractedReceipt(), backend="b", model="", model_version="1")


def test_receipt_round_trips_through_dict():
    receipt = FakeExtractor().receipt
    assert ExtractedReceipt.from_dict(receipt.to_dict()) == receipt


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def finish_job(conn, receipt_id, extractor):
    job_id = queue.enqueue(conn, receipt_id)
    queue.claim(conn, "w")
    result = extractor.extract([])
    return queue.complete(
        conn,
        job_id,
        "w",
        backend=result.backend,
        model=result.model,
        model_version=result.model_version,
        raw_output=result.raw_output,
        after=lambda c, rid, eid: extraction_store.save_receipt(c, eid, rid, result.receipt),
    )


def new_receipt(conn, status="unreviewed"):
    return conn.execute(
        "INSERT INTO receipts (origin, review_status) VALUES ('extracted', ?)", (status,)
    ).lastrowid


def test_rerun_keeps_earlier_extraction_for_comparison(conn):
    receipt_id = new_receipt(conn)
    first = finish_job(conn, receipt_id, FakeExtractor(model_version="1"))
    other = ExtractedReceipt(lines=(ExtractedLine("MILK", 210),))
    second = finish_job(conn, receipt_id, FakeExtractor(other, model_version="2"))

    assert extraction_store.extractions_for(conn, receipt_id) == [first, second]
    counts = dict(
        conn.execute("SELECT extraction_id, COUNT(*) FROM line_items GROUP BY extraction_id")
    )
    assert counts == {first: 2, second: 1}


def test_header_is_filled_but_never_blanked_by_a_later_run(conn):
    receipt_id = new_receipt(conn)
    finish_job(conn, receipt_id, FakeExtractor())
    finish_job(conn, receipt_id, FakeExtractor(ExtractedReceipt(ExtractedHeader(total_cents=350))))
    row = conn.execute(
        "SELECT s.name, r.total_cents FROM receipts r JOIN stores s ON s.id = r.store_id"
    ).fetchone()
    assert (row["name"], row["total_cents"]) == ("Fake Store", 350)


def test_accepted_receipt_header_is_not_overwritten(conn):
    receipt_id = new_receipt(conn, "accepted")
    conn.execute("UPDATE receipts SET total_cents = 999 WHERE id = ?", (receipt_id,))
    finish_job(conn, receipt_id, FakeExtractor())
    assert conn.execute("SELECT total_cents FROM receipts").fetchone()[0] == 999
    assert conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 2


def test_failure_while_storing_leaves_job_claimed(conn):
    receipt_id = new_receipt(conn)
    job_id = queue.enqueue(conn, receipt_id)
    queue.claim(conn, "w")

    def boom(c, rid, eid):
        raise sqlite3.OperationalError("disk full")

    with pytest.raises(sqlite3.OperationalError):
        queue.complete(conn, job_id, "w", backend="b", model="m", model_version="1", after=boom)
    assert queue.list_jobs(conn)[0].state == "claimed"
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0
