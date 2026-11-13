"""Job queue API used by extraction workers and the status views."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from receipt_tracker import queue


class ClaimRequest(BaseModel):
    worker: str = Field(min_length=1)
    backends: list[str] | None = None
    lease_seconds: int = Field(default=queue.DEFAULT_LEASE_SECONDS, ge=10, le=3600)


class WorkerRequest(BaseModel):
    worker: str = Field(min_length=1)
    lease_seconds: int = Field(default=queue.DEFAULT_LEASE_SECONDS, ge=10, le=3600)


class CompleteRequest(BaseModel):
    worker: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    raw_output: str | None = None


class FailRequest(BaseModel):
    worker: str = Field(min_length=1)
    error: str
    retry: bool = False


def build_router(
    open_db: Callable[[], sqlite3.Connection], archive_dir: Callable[[], Path]
) -> APIRouter:
    router = APIRouter()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = open_db()
        try:
            yield conn
        finally:
            conn.close()

    Conn = Depends(get_conn)

    def refuse(error: queue.QueueError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(error))

    def describe(conn: sqlite3.Connection, job: queue.Job) -> dict:
        photos = conn.execute(
            "SELECT id, sequence, original_filename FROM photos"
            " WHERE receipt_id = ? AND deleted_at IS NULL ORDER BY sequence, id",
            (job.receipt_id,),
        )
        return {**job.__dict__, "photos": [dict(row) for row in photos]}

    @router.get("/jobs/status")
    def jobs_status(conn: sqlite3.Connection = Conn) -> dict[str, int]:
        return queue.status(conn)

    @router.get("/jobs")
    def jobs_list(
        state: str | None = None, limit: int = 100, conn: sqlite3.Connection = Conn
    ) -> list[dict]:
        return [describe(conn, job) for job in queue.list_jobs(conn, state, min(limit, 500))]

    @router.post("/jobs/claim", response_model=None)
    def jobs_claim(body: ClaimRequest, conn: sqlite3.Connection = Conn) -> Response | dict:
        job = queue.claim(
            conn, body.worker, backends=body.backends, lease_seconds=body.lease_seconds
        )
        if job is None:
            return Response(status_code=204)
        return describe(conn, job)

    @router.post("/jobs/{job_id}/heartbeat")
    def jobs_heartbeat(job_id: int, body: WorkerRequest, conn: sqlite3.Connection = Conn) -> dict:
        try:
            queue.heartbeat(conn, job_id, body.worker, lease_seconds=body.lease_seconds)
        except queue.QueueError as error:
            raise refuse(error) from error
        return {"ok": True}

    @router.post("/jobs/{job_id}/complete")
    def jobs_complete(job_id: int, body: CompleteRequest, conn: sqlite3.Connection = Conn) -> dict:
        try:
            extraction_id = queue.complete(
                conn,
                job_id,
                body.worker,
                backend=body.backend,
                model=body.model,
                model_version=body.model_version,
                raw_output=body.raw_output,
            )
        except queue.QueueError as error:
            raise refuse(error) from error
        return {"extraction_id": extraction_id}

    @router.post("/jobs/{job_id}/fail")
    def jobs_fail(job_id: int, body: FailRequest, conn: sqlite3.Connection = Conn) -> dict:
        try:
            state = queue.fail(conn, job_id, body.worker, body.error, retry=body.retry)
        except queue.QueueError as error:
            raise refuse(error) from error
        return {"state": state}

    @router.get("/photos/{photo_id}/file")
    def photo_file(photo_id: int, conn: sqlite3.Connection = Conn) -> FileResponse:
        row = conn.execute(
            "SELECT archive_path, original_filename FROM photos"
            " WHERE id = ? AND deleted_at IS NULL",
            (photo_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such photo")
        root = archive_dir().resolve()
        path = (root / row["archive_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="Photo file is missing")
        return FileResponse(path, filename=row["original_filename"])

    return router
