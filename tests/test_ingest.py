import hashlib
import os

import pytest
from typer.testing import CliRunner

from receipt_tracker import db
from receipt_tracker.cli import app
from receipt_tracker.config import get_settings
from receipt_tracker.ingest import IngestReport, scan, watch

JPEG = b"\xff\xd8\xff\xe0"
NOW = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    folder.mkdir()
    return folder


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "archive"


def photo(folder, name, payload=b"one", *, head=JPEG, age=60.0):
    path = folder / name
    path.write_bytes(head + payload)
    os.utime(path, (NOW - age, NOW - age))
    return path


def run(conn, inbox, archive, **kwargs):
    return scan(conn, inbox, archive, now=NOW, **kwargs)


def rows(conn, sql):
    return [tuple(row) for row in conn.execute(sql)]


def test_photo_is_archived_recorded_and_queued(conn, inbox, archive):
    source = photo(inbox, "Receipt.JPG")
    digest = hashlib.sha256(JPEG + b"one").hexdigest()

    report = run(conn, inbox, archive)

    assert [(i.name, i.sequence, i.joined) for i in report.ingested] == [("Receipt.JPG", 1, False)]
    assert not source.exists()
    archived = archive / "2027" / "01" / f"{digest}.jpg"
    assert archived.read_bytes() == JPEG + b"one"
    assert rows(conn, "SELECT origin, review_status, store_id FROM receipts") == [
        ("extracted", "unreviewed", None)
    ]
    assert rows(
        conn,
        "SELECT receipt_id, sequence, sha256, original_filename, archive_path, byte_size,"
        " received_at FROM photos",
    ) == [(1, 1, digest, "Receipt.JPG", f"2027/01/{digest}.jpg", 7, "2027-01-15T08:00:00Z")]
    assert rows(conn, "SELECT receipt_id, state FROM jobs") == [(1, "queued")]


def test_each_photo_gets_its_own_receipt(conn, inbox, archive):
    photo(inbox, "a.jpg", b"a")
    photo(inbox, "b.png", b"b", head=b"\x89PNG\r\n\x1a\n")
    photo(inbox, "c.heic", b"c", head=b"\x00\x00\x00\x18ftypheic")
    photo(inbox, "d.webp", b"", head=b"RIFF\x00\x00\x00\x00WEBP")

    report = run(conn, inbox, archive)

    assert len(report.ingested) == 4
    assert rows(conn, "SELECT count(*) FROM receipts") == [(4,)]
    assert rows(conn, "SELECT count(*) FROM jobs") == [(4,)]
    assert list(inbox.iterdir()) == []


def test_duplicate_content_is_removed_without_new_rows(conn, inbox, archive):
    photo(inbox, "first.jpg")
    run(conn, inbox, archive)
    again = photo(inbox, "renamed copy.jpg")

    report = run(conn, inbox, archive)

    assert report.duplicates == ["renamed copy.jpg"]
    assert report.ingested == []
    assert not again.exists()
    assert rows(conn, "SELECT count(*) FROM photos") == [(1,)]
    assert rows(conn, "SELECT count(*) FROM receipts") == [(1,)]


def test_duplicate_restores_a_missing_archive_file(conn, inbox, archive):
    photo(inbox, "first.jpg")
    run(conn, inbox, archive)
    (relative,) = rows(conn, "SELECT archive_path FROM photos")[0]
    (archive / relative).unlink()
    photo(inbox, "first.jpg")

    run(conn, inbox, archive)

    assert (archive / relative).exists()


def test_duplicate_does_not_restore_a_photo_removed_by_retention(conn, inbox, archive):
    photo(inbox, "first.jpg")
    run(conn, inbox, archive)
    (relative,) = rows(conn, "SELECT archive_path FROM photos")[0]
    (archive / relative).unlink()
    conn.execute("UPDATE photos SET deleted_at = '2030-01-01T00:00:00Z'")
    photo(inbox, "first.jpg")

    report = run(conn, inbox, archive)

    assert report.duplicates == ["first.jpg"]
    assert not (archive / relative).exists()


def test_recently_modified_and_empty_files_wait(conn, inbox, archive):
    fresh = photo(inbox, "fresh.jpg", age=1.0)
    empty = photo(inbox, "empty.jpg", b"", head=b"")

    report = run(conn, inbox, archive, settle_seconds=5.0)

    assert report.waiting == ["empty.jpg", "fresh.jpg"]
    assert fresh.exists() and empty.exists()
    assert rows(conn, "SELECT count(*) FROM photos") == [(0,)]


def test_unsupported_and_fake_images_are_left_in_place(conn, inbox, archive):
    notes = photo(inbox, "notes.txt")
    fake = photo(inbox, "fake.jpg", head=b"not an image")

    report = run(conn, inbox, archive)

    assert report.skipped == [
        ("fake.jpg", "the content is not a recognised image"),
        ("notes.txt", "not a supported image type"),
    ]
    assert notes.exists() and fake.exists()
    assert rows(conn, "SELECT count(*) FROM receipts") == [(0,)]


def test_hidden_files_and_subfolders_are_ignored(conn, inbox, archive):
    photo(inbox, ".DS_Store")
    photo(inbox, ".hidden.jpg")
    (inbox / "sub").mkdir()
    photo(inbox / "sub", "nested.jpg")

    report = run(conn, inbox, archive)

    assert report.lines() == []
    assert (inbox / "sub" / "nested.jpg").exists()


def test_missing_watch_folder_is_created(conn, tmp_path, archive):
    report = run(conn, tmp_path / "not-yet", archive)
    assert report.lines() == []
    assert (tmp_path / "not-yet").is_dir()


def test_numbered_parts_join_one_receipt(conn, inbox, archive):
    photo(inbox, "Trip-2.jpg", b"second")
    photo(inbox, "trip-1.jpg", b"first")

    report = run(conn, inbox, archive)

    assert [(i.name, i.sequence, i.joined) for i in report.ingested] == [
        ("trip-1.jpg", 1, False),
        ("Trip-2.jpg", 2, True),
    ]
    assert rows(conn, "SELECT count(*) FROM receipts") == [(1,)]
    assert rows(conn, "SELECT count(*) FROM jobs") == [(1,)]
    assert rows(conn, "SELECT sequence FROM photos ORDER BY sequence") == [(1,), (2,)]


def test_part_arriving_in_a_later_scan_still_joins(conn, inbox, archive):
    photo(inbox, "trip-1.jpg", b"first")
    run(conn, inbox, archive)
    photo(inbox, "trip-2.jpg", b"second")

    report = run(conn, inbox, archive)

    assert report.ingested[0].joined
    assert rows(conn, "SELECT count(*) FROM receipts") == [(1,)]


def test_part_arriving_after_extraction_started_is_a_new_receipt(conn, inbox, archive):
    photo(inbox, "trip-1.jpg", b"first")
    run(conn, inbox, archive)
    conn.execute("UPDATE jobs SET state = 'claimed'")
    photo(inbox, "trip-2.jpg", b"second")

    report = run(conn, inbox, archive)

    assert not report.ingested[0].joined
    assert rows(conn, "SELECT count(*) FROM receipts") == [(2,)]


def test_same_part_number_with_new_content_is_a_new_receipt(conn, inbox, archive):
    photo(inbox, "trip-1.jpg", b"first")
    run(conn, inbox, archive)
    photo(inbox, "trip-1.jpg", b"retake")

    run(conn, inbox, archive)

    assert rows(conn, "SELECT count(*) FROM receipts") == [(2,)]


def test_unrelated_numbered_names_do_not_join(conn, inbox, archive):
    photo(inbox, "trip-1.jpg", b"a")
    photo(inbox, "other-2.jpg", b"b")
    photo(inbox, "code 1234-006.jpg", b"c")

    run(conn, inbox, archive)

    assert rows(conn, "SELECT count(*) FROM receipts") == [(3,)]


def test_report_lines(conn, inbox, archive):
    photo(inbox, "a-1.jpg", b"a")
    photo(inbox, "a-2.jpg", b"b")
    photo(inbox, "notes.txt")
    photo(inbox, "fresh.jpg", age=0.0)

    report = run(conn, inbox, archive)

    assert report.lines() == [
        "a-1.jpg: receipt 1, queued for extraction",
        "a-2.jpg: added to receipt 1 as photo 2",
        "fresh.jpg: still being written, will retry",
        "notes.txt: left in place, not a supported image type",
    ]


def test_watch_reports_a_file_left_in_place_only_once():
    reports = [
        IngestReport(skipped=[("notes.txt", "not a supported image type")]),
        IngestReport(skipped=[("notes.txt", "not a supported image type")]),
        IngestReport(duplicates=["again.jpg"]),
        IngestReport(skipped=[("notes.txt", "not a supported image type")]),
    ]
    emitted, naps = [], []

    watch(lambda: reports.pop(0), emitted.append, interval=3, max_scans=4, sleep=naps.append)

    assert emitted == [
        "notes.txt: left in place, not a supported image type",
        "again.jpg: already archived, removed from the watched folder",
        "notes.txt: left in place, not a supported image type",
    ]
    assert naps == [3, 3, 3]


def test_cli_ingest(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("RECEIPT_TRACKER_WATCH_SETTLE_SECONDS", "0")
    get_settings.cache_clear()
    try:
        inbox = tmp_path / "var" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "receipt.jpg").write_bytes(JPEG + b"x")
        runner = CliRunner()
        first = runner.invoke(app, ["ingest"])
        second = runner.invoke(app, ["ingest"])
    finally:
        get_settings.cache_clear()
    assert first.exit_code == 0
    assert "receipt.jpg: receipt 1, queued for extraction" in first.output
    assert second.exit_code == 0
    assert "nothing to ingest" in second.output
    assert len(list((tmp_path / "var" / "archive").rglob("*.jpg"))) == 1
