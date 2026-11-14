import pytest
from fastapi.testclient import TestClient

from receipt_tracker import db, queue, worker
from receipt_tracker.extractors import BackendUnavailable
from receipt_tracker.extractors.fake import FakeExtractor
from receipt_tracker.web.app import create_app


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "test.db"
    archive = tmp_path / "archive"
    archive.mkdir()
    conn = db.connect(db_path)
    db.migrate(conn)
    client = TestClient(create_app(db_path=db_path, archive_dir=archive))
    yield conn, client, archive
    conn.close()


def add_job_with_photo(conn, archive, content=b"photo-bytes", name="a.heic"):
    receipt_id = conn.execute("INSERT INTO receipts (origin) VALUES ('extracted')").lastrowid
    (archive / name).write_bytes(content)
    photo_id = conn.execute(
        "INSERT INTO photos (receipt_id, sha256, original_filename, archive_path, byte_size)"
        " VALUES (?, ?, ?, ?, ?)",
        (receipt_id, name, name, name, len(content)),
    ).lastrowid
    return queue.enqueue(conn, receipt_id), photo_id


def test_claim_complete_flow(env):
    conn, client, archive = env
    job_id, photo_id = add_job_with_photo(conn, archive)

    response = client.post("/jobs/claim", json={"worker": "w1"})
    body = response.json()
    assert (body["id"], body["state"], body["photos"][0]["id"]) == (job_id, "claimed", photo_id)

    assert client.get(f"/photos/{photo_id}/file").content == b"photo-bytes"
    done = client.post(
        f"/jobs/{job_id}/complete",
        json={"worker": "w1", "backend": "vision", "model": "m", "model_version": "1"},
    )
    assert done.status_code == 200
    assert client.get("/jobs/status").json()["done"] == 1


def test_claim_when_empty_is_204(env):
    _, client, _ = env
    assert client.post("/jobs/claim", json={"worker": "w1"}).status_code == 204


def test_second_worker_cannot_claim_or_finish_the_job(env):
    conn, client, archive = env
    job_id, _ = add_job_with_photo(conn, archive)
    client.post("/jobs/claim", json={"worker": "w1"})
    assert client.post("/jobs/claim", json={"worker": "w2"}).status_code == 204
    other = client.post(
        f"/jobs/{job_id}/complete",
        json={"worker": "w2", "backend": "b", "model": "m", "model_version": "1"},
    )
    assert other.status_code == 409


def test_jobs_listing_filters_by_state(env):
    conn, client, archive = env
    add_job_with_photo(conn, archive)
    assert len(client.get("/jobs", params={"state": "queued"}).json()) == 1
    assert client.get("/jobs", params={"state": "done"}).json() == []


def test_photo_file_cannot_escape_archive(env):
    conn, client, archive = env
    (archive.parent / "secret.txt").write_text("nope")
    conn.execute(
        "INSERT INTO photos (sha256, original_filename, archive_path, byte_size)"
        " VALUES ('x', 'x', '../secret.txt', 4)"
    )
    photo_id = conn.execute("SELECT id FROM photos").fetchone()[0]
    assert client.get(f"/photos/{photo_id}/file").status_code == 404


class InProcessClient(worker.QueueClient):
    """A QueueClient that talks to the app through TestClient instead of a socket."""

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


def test_worker_runs_job_and_records_result(env):
    conn, client, archive = env
    add_job_with_photo(conn, archive)
    seen = []

    class Reading(FakeExtractor):
        def extract(self, photos):
            seen.extend(p.read_bytes() for p in photos)  # temp files vanish after the job
            return super().extract(photos)

    extractor = Reading()

    assert worker.run_once(InProcessClient(client), extractor, "w1") is True
    assert seen == [b"photo-bytes"]
    assert queue.list_jobs(conn)[0].state == "done"
    assert worker.run_once(InProcessClient(client), extractor, "w1") is False
    lines = conn.execute("SELECT raw_text FROM line_items ORDER BY position").fetchall()
    assert [row["raw_text"] for row in lines] == ["MILK", "BREAD"]
    assert conn.execute("SELECT total_cents FROM receipts").fetchone()[0] == 300


def test_worker_requeues_when_backend_unavailable(env):
    conn, client, archive = env
    add_job_with_photo(conn, archive)
    worker.run_once(
        InProcessClient(client), Raising(BackendUnavailable("connection refused")), "w1"
    )
    job = queue.list_jobs(conn)[0]
    assert job.state == "queued"
    assert "connection refused" in job.error


def test_worker_fails_job_on_extractor_error(env):
    conn, client, archive = env
    add_job_with_photo(conn, archive)
    worker.run_once(InProcessClient(client), Raising(ValueError("unreadable")), "w1")
    job = queue.list_jobs(conn)[0]
    assert (job.state, job.error) == ("failed", "ValueError: unreadable")


def test_invalid_receipt_payload_is_rejected_and_job_stays_claimed(env):
    conn, client, archive = env
    job_id, _ = add_job_with_photo(conn, archive)
    client.post("/jobs/claim", json={"worker": "w1"})
    response = client.post(
        f"/jobs/{job_id}/complete",
        json={
            "worker": "w1",
            "backend": "b",
            "model": "m",
            "model_version": "1",
            "receipt": {"lines": [{"raw_text": "X", "line_price_cents": 1.5}]},
        },
    )
    assert response.status_code == 422
    assert queue.list_jobs(conn)[0].state == "claimed"
