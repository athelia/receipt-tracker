"""Extraction job queue on top of the `jobs` table.

Workers (possibly on other machines) claim jobs through the API. A claim is a
lease: it expires unless the worker renews it, so a worker that sleeps or
vanishes does not strand a job. Every state change is a single conditional
UPDATE, so two workers can never hold the same job.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 3


class QueueError(Exception):
    """A queue operation was refused, e.g. the caller does not hold the claim."""


@dataclass(frozen=True)
class Job:
    id: int
    receipt_id: int
    state: str
    requested_backend: str | None
    claimed_by: str | None
    claim_expires_at: str | None
    attempts: int
    error: str | None


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(UTC)


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        receipt_id=row["receipt_id"],
        state=row["state"],
        requested_backend=row["requested_backend"],
        claimed_by=row["claimed_by"],
        claim_expires_at=row["claim_expires_at"],
        attempts=row["attempts"],
        error=row["error"],
    )


def enqueue(conn: sqlite3.Connection, receipt_id: int, requested_backend: str | None = None) -> int:
    """Queue a receipt for extraction. Returns the new job id."""
    cursor = conn.execute(
        "INSERT INTO jobs (receipt_id, requested_backend) VALUES (?, ?)",
        (receipt_id, requested_backend),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def claim(
    conn: sqlite3.Connection,
    worker: str,
    *,
    backends: list[str] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> Job | None:
    """Claim the oldest claimable job, or return None if there is none.

    Claimable means queued, or claimed with an expired lease. A job whose
    lease expired `max_attempts` times is marked failed instead of retried
    forever. When `backends` is given, only jobs that request one of them, or
    no particular backend, are eligible.
    """
    moment = now or _now()
    stamp = _timestamp(moment)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _fail_exhausted(conn, stamp, max_attempts)
        clause, params = "", []
        if backends is not None:
            marks = ", ".join("?" for _ in backends)
            clause = f" AND (requested_backend IS NULL OR requested_backend IN ({marks}))"
            params = list(backends)
        row = conn.execute(
            f"""
            SELECT id FROM jobs
            WHERE (state = 'queued' OR (state = 'claimed' AND claim_expires_at <= ?)){clause}
            ORDER BY created_at, id
            LIMIT 1
            """,
            [stamp, *params],
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            """
            UPDATE jobs
            SET state = 'claimed', claimed_by = ?, claimed_at = ?, claim_expires_at = ?,
                attempts = attempts + 1, error = NULL
            WHERE id = ?
            """,
            (worker, stamp, _timestamp(moment + timedelta(seconds=lease_seconds)), row["id"]),
        )
        job = _job(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
        conn.execute("COMMIT")
        return job
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _fail_exhausted(conn: sqlite3.Connection, stamp: str, max_attempts: int) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET state = 'failed', finished_at = ?, error = 'Claim expired too many times'
        WHERE state = 'claimed' AND claim_expires_at <= ? AND attempts >= ?
        """,
        (stamp, stamp, max_attempts),
    )


def _require_claim(conn: sqlite3.Connection, job_id: int, worker: str, now: datetime) -> None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"No such job: {job_id}")
    if row["state"] != "claimed" or row["claimed_by"] != worker:
        raise QueueError(f"Job {job_id} is not claimed by {worker}")
    if row["claim_expires_at"] <= _timestamp(now):
        raise QueueError(f"Claim on job {job_id} has expired")


def heartbeat(
    conn: sqlite3.Connection,
    job_id: int,
    worker: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> None:
    """Extend the lease on a job the worker still holds."""
    moment = now or _now()
    cursor = conn.execute(
        """
        UPDATE jobs SET claim_expires_at = ?
        WHERE id = ? AND state = 'claimed' AND claimed_by = ? AND claim_expires_at > ?
        """,
        (_timestamp(moment + timedelta(seconds=lease_seconds)), job_id, worker, _timestamp(moment)),
    )
    if cursor.rowcount == 0:
        _require_claim(conn, job_id, worker, moment)


def complete(
    conn: sqlite3.Connection,
    job_id: int,
    worker: str,
    *,
    backend: str,
    model: str,
    model_version: str,
    raw_output: str | None = None,
    now: datetime | None = None,
) -> int:
    """Mark a job done and record the extraction with its provenance.

    Returns the extraction id. Parsing `raw_output` into line items is the
    job of the extractor pipeline, not the queue.
    """
    moment = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_claim(conn, job_id, worker, moment)
        receipt_id = conn.execute("SELECT receipt_id FROM jobs WHERE id = ?", (job_id,)).fetchone()[
            0
        ]
        cursor = conn.execute(
            """
            INSERT INTO extractions (receipt_id, job_id, backend, model, model_version, raw_output)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (receipt_id, job_id, backend, model, model_version, raw_output),
        )
        conn.execute(
            "UPDATE jobs SET state = 'done', finished_at = ?, error = NULL WHERE id = ?",
            (_timestamp(moment), job_id),
        )
        conn.execute("COMMIT")
        assert cursor.lastrowid is not None
        return cursor.lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fail(
    conn: sqlite3.Connection,
    job_id: int,
    worker: str,
    error: str,
    *,
    retry: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> str:
    """Report a failed attempt. Returns the job's new state.

    With `retry` the job goes back to the queue unless it is out of attempts,
    which is how a worker reports "model server unreachable, not the receipt's
    fault". Otherwise the job is failed for good.
    """
    moment = now or _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_claim(conn, job_id, worker, moment)
        attempts = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        if retry and attempts < max_attempts:
            conn.execute(
                """
                UPDATE jobs
                SET state = 'queued', claimed_by = NULL, claimed_at = NULL,
                    claim_expires_at = NULL, error = ?
                WHERE id = ?
                """,
                (error, job_id),
            )
            state = "queued"
        else:
            conn.execute(
                "UPDATE jobs SET state = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (_timestamp(moment), error, job_id),
            )
            state = "failed"
        conn.execute("COMMIT")
        return state
    except Exception:
        conn.execute("ROLLBACK")
        raise


def requeue(conn: sqlite3.Connection, job_id: int) -> None:
    """Put a failed or done job back in the queue (for a re-run)."""
    cursor = conn.execute(
        """
        UPDATE jobs
        SET state = 'queued', attempts = 0, claimed_by = NULL, claimed_at = NULL,
            claim_expires_at = NULL, error = NULL, finished_at = NULL
        WHERE id = ? AND state IN ('done', 'failed')
        """,
        (job_id,),
    )
    if cursor.rowcount == 0:
        raise QueueError(f"Job {job_id} is not done or failed")


def status(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, int]:
    """Count jobs by state. Claims past their lease count as `expired`."""
    stamp = _timestamp(now or _now())
    counts = {"queued": 0, "claimed": 0, "expired": 0, "done": 0, "failed": 0}
    rows = conn.execute(
        """
        SELECT CASE WHEN state = 'claimed' AND claim_expires_at <= ? THEN 'expired' ELSE state END
               AS bucket, COUNT(*) AS n
        FROM jobs GROUP BY bucket
        """,
        (stamp,),
    )
    for row in rows:
        counts[row["bucket"]] = row["n"]
    return counts


def list_jobs(conn: sqlite3.Connection, state: str | None = None, limit: int = 100) -> list[Job]:
    sql = "SELECT * FROM jobs"
    params: list[object] = []
    if state is not None:
        sql += " WHERE state = ?"
        params.append(state)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [_job(row) for row in conn.execute(sql, params)]
