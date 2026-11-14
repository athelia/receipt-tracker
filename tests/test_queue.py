import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from receipt_tracker import db, queue

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    yield connection
    connection.close()


def add_receipt(conn):
    return conn.execute("INSERT INTO receipts (origin) VALUES ('extracted')").lastrowid


def add_job(conn, backend=None):
    return queue.enqueue(conn, add_receipt(conn), backend)


def test_claim_returns_oldest_and_none_when_empty(conn):
    assert queue.claim(conn, "a") is None
    first = add_job(conn)
    add_job(conn)
    job = queue.claim(conn, "a", now=T0)
    assert (job.id, job.state, job.claimed_by, job.attempts) == (first, "claimed", "a", 1)


def test_claimed_job_is_not_claimable_by_another_worker(conn):
    add_job(conn)
    assert queue.claim(conn, "a", now=T0) is not None
    assert queue.claim(conn, "b", now=T0 + timedelta(seconds=10)) is None


def test_expired_claim_becomes_claimable_again(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", lease_seconds=60, now=T0)
    job = queue.claim(conn, "b", now=T0 + timedelta(seconds=61))
    assert (job.id, job.claimed_by, job.attempts) == (job_id, "b", 2)


def test_expired_worker_cannot_complete_or_heartbeat(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", lease_seconds=60, now=T0)
    late = T0 + timedelta(seconds=61)
    with pytest.raises(queue.QueueError):
        queue.heartbeat(conn, job_id, "a", now=late)
    with pytest.raises(queue.QueueError):
        queue.complete(conn, job_id, "a", backend="b", model="m", model_version="1", now=late)


def test_stale_worker_loses_to_new_claimant(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", lease_seconds=60, now=T0)
    later = T0 + timedelta(seconds=61)
    queue.claim(conn, "b", now=later)
    with pytest.raises(queue.QueueError):
        queue.complete(conn, job_id, "a", backend="x", model="m", model_version="1", now=later)


def test_heartbeat_extends_lease(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", lease_seconds=60, now=T0)
    queue.heartbeat(conn, job_id, "a", lease_seconds=60, now=T0 + timedelta(seconds=50))
    assert queue.claim(conn, "b", now=T0 + timedelta(seconds=70)) is None


def test_complete_records_extraction_with_provenance(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", now=T0)
    extraction_id = queue.complete(
        conn, job_id, "a", backend="vision", model="m", model_version="v1", raw_output="{}", now=T0
    )
    row = conn.execute("SELECT * FROM extractions WHERE id = ?", (extraction_id,)).fetchone()
    assert (row["job_id"], row["backend"], row["model"], row["model_version"]) == (
        job_id,
        "vision",
        "m",
        "v1",
    )
    assert queue.status(conn, now=T0)["done"] == 1


def test_complete_requires_provenance(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        queue.complete(conn, job_id, "a", backend="", model="m", model_version="1", now=T0)
    assert queue.status(conn, now=T0)["claimed"] == 1


def test_fail_with_retry_requeues_until_out_of_attempts(conn):
    job_id = add_job(conn)
    for attempt in range(1, 4):
        queue.claim(conn, "a", now=T0)
        state = queue.fail(conn, job_id, "a", "model down", retry=True, max_attempts=3, now=T0)
        assert state == ("queued" if attempt < 3 else "failed")


def test_fail_without_retry_is_final(conn):
    job_id = add_job(conn)
    queue.claim(conn, "a", now=T0)
    assert queue.fail(conn, job_id, "a", "unreadable", now=T0) == "failed"
    assert queue.claim(conn, "b", now=T0) is None


def test_job_with_repeatedly_expiring_claims_fails(conn):
    add_job(conn)
    moment = T0
    for _ in range(3):
        assert queue.claim(conn, "a", lease_seconds=10, max_attempts=3, now=moment) is not None
        moment += timedelta(seconds=11)
    assert queue.claim(conn, "a", max_attempts=3, now=moment) is None
    assert queue.status(conn, now=moment)["failed"] == 1


def test_backend_filter(conn):
    add_job(conn, "vision")
    assert queue.claim(conn, "a", backends=["ocr"], now=T0) is None
    assert queue.claim(conn, "a", backends=["vision"], now=T0) is not None


def test_requeue_only_finished_jobs(conn):
    job_id = add_job(conn)
    with pytest.raises(queue.QueueError):
        queue.requeue(conn, job_id)
    queue.claim(conn, "a", now=T0)
    queue.fail(conn, job_id, "a", "x", now=T0)
    queue.requeue(conn, job_id)
    assert queue.claim(conn, "b", now=T0).attempts == 1


def test_status_counts_expired_separately(conn):
    add_job(conn)
    add_job(conn)
    queue.claim(conn, "a", lease_seconds=10, now=T0)
    counts = queue.status(conn, now=T0 + timedelta(seconds=11))
    assert counts == {"queued": 1, "claimed": 0, "expired": 1, "done": 0, "failed": 0}


def test_concurrent_workers_never_share_a_job(tmp_path):
    path = tmp_path / "race.db"
    setup = db.connect(path)
    db.migrate(setup)
    for _ in range(20):
        add_job(setup)
    setup.close()

    claimed, lock = [], threading.Lock()

    def work(name):
        connection = db.connect(path)
        connection.execute("PRAGMA busy_timeout = 10000")
        while (job := queue.claim(connection, name)) is not None:
            with lock:
                claimed.append(job.id)
        connection.close()

    threads = [threading.Thread(target=work, args=(f"w{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == sorted(set(claimed))
    assert len(claimed) == 20
