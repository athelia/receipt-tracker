import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from receipt_tracker import db, logging_setup, queue, worker
from receipt_tracker.config import Settings
from receipt_tracker.extractors import BackendUnavailable, ExtractionError
from receipt_tracker.extractors.fake import FakeExtractor
from receipt_tracker.web.app import create_app


def events(read, name):
    return [entry for entry in read() if entry["event"] == name]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    yield connection
    connection.close()


def add_receipt(conn):
    return conn.execute("INSERT INTO receipts (origin) VALUES ('extracted')").lastrowid


# --- the log file itself -----------------------------------------------------


def test_log_lines_are_json_with_utc_timestamp_and_fields(settings, log):
    logging_setup.event("something_happened", job_id=7, worker="w1")
    (entry,) = log()
    assert entry["event"] == "something_happened"
    assert (entry["job_id"], entry["worker"], entry["level"]) == (7, "w1", "INFO")
    assert entry["ts"].endswith("Z")


def test_each_process_gets_its_own_file(settings):
    app_log = logging_setup.configure(settings, "app")
    worker_log = logging_setup.configure(settings, "worker")
    assert (app_log.name, worker_log.name) == ("app.log", "worker.log")
    assert app_log.parent == settings.log_dir == settings.data_dir / "logs"


def test_configuring_twice_does_not_duplicate_lines(settings, log):
    logging_setup.configure(settings, "test")
    logging_setup.event("once")
    assert len(events(log, "once")) == 1


def test_log_rotates_at_the_configured_size(tmp_path):
    settings = Settings(data_dir=tmp_path, log_max_bytes=400, log_backup_count=2, _env_file=None)
    path = logging_setup.configure(settings, "rotating")
    try:
        for number in range(40):
            logging_setup.event("filler", number=number, padding="x" * 40)
    finally:
        for handler in logging.getLogger(logging_setup.ROOT).handlers:
            handler.close()
    assert path.with_name("rotating.log.1").exists()
    assert not path.with_name("rotating.log.3").exists()


def test_level_setting_filters_events(tmp_path):
    settings = Settings(data_dir=tmp_path, log_level="WARNING", _env_file=None)
    path = logging_setup.configure(settings, "quiet")
    logging_setup.event("routine")
    logging_setup.event("trouble", level=logging.ERROR)
    logging.getLogger(logging_setup.ROOT).handlers[-1].flush()
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == ["trouble"]
    logging.getLogger(logging_setup.ROOT).setLevel(logging.NOTSET)


# --- queue events -------------------------------------------------------------


def test_job_lifecycle_is_logged_with_the_job_id(conn, log):
    job_id = queue.enqueue(conn, add_receipt(conn))
    queue.claim(conn, "w1")
    queue.complete(conn, job_id, "w1", backend="b", model="m", model_version="1")
    names = [entry["event"] for entry in log()]
    assert names == ["job_enqueued", "job_claimed", "job_completed"]
    assert {entry["job_id"] for entry in log()} == {job_id}
    assert events(log, "job_claimed")[0]["attempt"] == 1


def test_requeue_and_failure_are_logged_as_different_events(conn, log):
    first = queue.enqueue(conn, add_receipt(conn))
    queue.claim(conn, "w1")
    queue.fail(conn, first, "w1", "Backend unavailable: refused", retry=True)
    second = queue.enqueue(conn, add_receipt(conn))
    queue.claim(conn, "w1")  # takes the older, requeued job
    queue.fail(conn, first, "w1", "ExtractionError: bad", retry=False)

    (requeued,) = events(log, "job_requeued")
    (failed,) = events(log, "job_failed")
    assert (requeued["job_id"], requeued["retry"], requeued["level"]) == (first, True, "INFO")
    assert (failed["job_id"], failed["error"], failed["level"]) == (
        first,
        "ExtractionError: bad",
        "WARNING",
    )
    assert second not in {entry.get("job_id") for entry in events(log, "job_failed")}


def test_expired_lease_and_exhausted_job_are_logged(conn, log):
    job_id = queue.enqueue(conn, add_receipt(conn))
    start = datetime.now(UTC)
    for attempt in range(queue.DEFAULT_MAX_ATTEMPTS):
        queue.claim(conn, f"w{attempt}", lease_seconds=10, now=start + timedelta(hours=attempt))
    queue.claim(conn, "w9", now=start + timedelta(hours=10))

    assert len(events(log, "lease_expired")) == queue.DEFAULT_MAX_ATTEMPTS - 1
    (failed,) = events(log, "job_failed")
    assert (failed["job_id"], failed["reason"]) == (job_id, "claim_expired_too_many_times")


def test_rolled_back_change_is_not_logged(conn, log):
    job_id = queue.enqueue(conn, add_receipt(conn))
    queue.claim(conn, "w1")

    def explode(*_):
        raise RuntimeError("storage failed")

    with pytest.raises(RuntimeError):
        queue.complete(conn, job_id, "w1", backend="b", model="m", model_version="1", after=explode)
    assert events(log, "job_completed") == []


# --- worker events ------------------------------------------------------------


class InProcessClient(worker.QueueClient):
    def __init__(self, test_client):
        super().__init__("http://testserver")
        self.http = test_client

    def _request(self, method, path, body=None):
        response = self.http.request(method, path, json=body)
        if response.status_code >= 400:
            raise worker.ApiError(response.status_code, response.text)
        return response.status_code, response.content


class Raising:
    def __init__(self, error):
        self.error = error

    def extract(self, photos):
        raise self.error


@pytest.fixture
def env(tmp_path, conn):
    archive = tmp_path / "archive"
    archive.mkdir()
    receipt_id = add_receipt(conn)
    (archive / "a.heic").write_bytes(b"x")
    conn.execute(
        "INSERT INTO photos (receipt_id, sha256, original_filename, archive_path, byte_size)"
        " VALUES (?, 'a', 'a.heic', 'a.heic', 1)",
        (receipt_id,),
    )
    job_id = queue.enqueue(conn, receipt_id)
    client = InProcessClient(TestClient(create_app(tmp_path / "test.db", archive)))
    return conn, client, job_id


def test_failed_extraction_logs_error_type_message_and_job_id(env, log):
    conn, client, job_id = env
    error = ExtractionError("line 2 line_price: not a number: 'abc'")
    worker.run_once(client, Raising(error), "w1")

    (entry,) = events(log, "extraction_failed")
    assert entry["job_id"] == job_id
    assert entry["error_type"] == "ExtractionError"
    assert "not a number" in entry["error"]
    assert (entry["level"], entry["photos"], entry["worker"]) == ("ERROR", 1, "w1")
    assert entry["duration_s"] >= 0
    assert queue.list_jobs(conn)[0].state == "failed"
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def test_unavailable_backend_is_logged_apart_from_failure(env, log):
    conn, client, job_id = env
    worker.run_once(client, Raising(BackendUnavailable("connection refused")), "w1")
    assert events(log, "extraction_failed") == []
    (entry,) = events(log, "extraction_unavailable")
    assert (entry["job_id"], entry["error_type"], entry["level"]) == (
        job_id,
        "BackendUnavailable",
        "WARNING",
    )
    assert events(log, "job_requeued")[0]["job_id"] == job_id


def test_successful_extraction_is_logged_with_provenance(env, log):
    conn, client, job_id = env
    worker.run_once(client, FakeExtractor(), "w1")
    (entry,) = events(log, "extraction_finished")
    assert (entry["job_id"], entry["backend"], entry["lines"]) == (job_id, "fake", 2)
    assert "model_version" in entry


# --- raw model output ----------------------------------------------------------


def test_raw_output_is_not_in_the_log_and_not_written_by_default(env, settings, log):
    conn, client, _ = env
    error = ExtractionError("bad", raw_output="SECRET RECEIPT TEXT")
    assert logging_setup.raw_failure_path(settings) is None
    worker.run_once(client, Raising(error), "w1", raw_failure_log=None)
    assert "SECRET RECEIPT TEXT" not in str(log())
    assert not (settings.log_dir / logging_setup.RAW_FAILURES_FILE).exists()


def test_raw_output_goes_to_its_own_file_when_enabled(env, tmp_path, log):
    conn, client, job_id = env
    settings = Settings(data_dir=tmp_path, log_raw_failures=True, _env_file=None)
    path = logging_setup.raw_failure_path(settings)
    error = ExtractionError("bad json", raw_output="SECRET RECEIPT TEXT")
    worker.run_once(client, Raising(error), "w1", raw_failure_log=path)

    (entry,) = [json.loads(line) for line in path.read_text().splitlines()]
    assert (entry["job_id"], entry["raw_output"]) == (job_id, "SECRET RECEIPT TEXT")
    assert "SECRET RECEIPT TEXT" not in str(log())  # still not in the event log


def test_failure_without_raw_output_writes_nothing_even_when_enabled(env, tmp_path):
    conn, client, _ = env
    settings = Settings(data_dir=tmp_path, log_raw_failures=True, _env_file=None)
    path = logging_setup.raw_failure_path(settings)
    worker.run_once(client, Raising(ValueError("unreadable")), "w1", raw_failure_log=path)
    assert not path.exists()


# --- job context -----------------------------------------------------------------


def test_events_inside_a_job_context_carry_its_job_id(log):
    logging_setup.event("outside")
    with logging_setup.job_context(41):
        logging_setup.event("inside")
        logging_setup.event("explicit", job_id=99)
    logging_setup.event("after")
    by_name = {entry["event"]: entry for entry in log()}
    assert "job_id" not in by_name["outside"] and "job_id" not in by_name["after"]
    assert by_name["inside"]["job_id"] == 41
    assert by_name["explicit"]["job_id"] == 99  # an explicit value wins


def test_extractor_events_get_the_job_id_without_knowing_it(env, log):
    conn, client, job_id = env

    class Chatty(FakeExtractor):
        def extract(self, photos):
            logging_setup.event("model_call", note="from inside the extractor")
            return super().extract(photos)

    worker.run_once(client, Chatty(), "w1")
    (entry,) = events(log, "model_call")
    assert entry["job_id"] == job_id
