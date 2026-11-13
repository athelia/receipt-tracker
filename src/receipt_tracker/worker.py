"""Extraction worker: claims jobs from the app's API and runs an extractor.

The worker talks to the app only over HTTP, so it can run on the NAS, the Mac
or beside the GPU PC's model server. It uses the standard library for HTTP to
avoid adding a dependency.

`Extractor` here is the minimum the worker needs. AA-54 defines the full
extractor interface; this protocol should be replaced by it then.
"""

import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BackendUnavailable(Exception):
    """The extractor's model server could not be reached. The job is retried."""


@dataclass(frozen=True)
class ExtractionResult:
    backend: str
    model: str
    model_version: str
    raw_output: str | None = None


class Extractor(Protocol):
    def __call__(self, photos: list[Path]) -> ExtractionResult: ...


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


class QueueClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise ApiError(error.code, detail) from error

    def claim(self, worker: str, backends: list[str] | None, lease_seconds: int) -> dict | None:
        body = {"worker": worker, "backends": backends, "lease_seconds": lease_seconds}
        status, payload = self._request("POST", "/jobs/claim", body)
        return None if status == 204 else json.loads(payload)

    def heartbeat(self, job_id: int, worker: str, lease_seconds: int) -> None:
        body = {"worker": worker, "lease_seconds": lease_seconds}
        self._request("POST", f"/jobs/{job_id}/heartbeat", body)

    def complete(self, job_id: int, worker: str, result: ExtractionResult) -> None:
        body = {
            "worker": worker,
            "backend": result.backend,
            "model": result.model,
            "model_version": result.model_version,
            "raw_output": result.raw_output,
        }
        self._request("POST", f"/jobs/{job_id}/complete", body)

    def fail(self, job_id: int, worker: str, error: str, retry: bool) -> None:
        body = {"worker": worker, "error": error, "retry": retry}
        self._request("POST", f"/jobs/{job_id}/fail", body)

    def download_photo(self, photo_id: int, dest: Path) -> None:
        _, payload = self._request("GET", f"/photos/{photo_id}/file")
        dest.write_bytes(payload)


def _keep_alive(
    client: QueueClient, job_id: int, worker: str, lease_seconds: int, stop: threading.Event
) -> None:
    while not stop.wait(lease_seconds / 3):
        try:
            client.heartbeat(job_id, worker, lease_seconds)
        except (ApiError, OSError):
            return  # Lost the claim or the app; the lease will lapse on its own.


def run_once(
    client: QueueClient,
    extractor: Extractor,
    worker: str,
    *,
    backends: list[str] | None = None,
    lease_seconds: int = 300,
) -> bool:
    """Claim and process one job. Returns False if there was nothing to do."""
    job = client.claim(worker, backends, lease_seconds)
    if job is None:
        return False
    stop = threading.Event()
    beat = threading.Thread(
        target=_keep_alive, args=(client, job["id"], worker, lease_seconds, stop), daemon=True
    )
    beat.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            photos = []
            for photo in job["photos"]:
                suffix = Path(photo["original_filename"]).suffix
                path = Path(folder) / f"{photo['id']}{suffix}"
                client.download_photo(photo["id"], path)
                photos.append(path)
            if not photos:
                raise ValueError("Receipt has no photos")
            result = extractor(photos)
    except (BackendUnavailable, OSError) as error:
        _report(client, job["id"], worker, f"Backend unavailable: {error}", retry=True)
    except Exception as error:  # noqa: BLE001 - any extractor failure is the job's failure
        _report(client, job["id"], worker, f"{type(error).__name__}: {error}", retry=False)
    else:
        client.complete(job["id"], worker, result)
    finally:
        stop.set()
    return True


def _report(client: QueueClient, job_id: int, worker: str, error: str, *, retry: bool) -> None:
    try:
        client.fail(job_id, worker, error, retry)
    except (ApiError, OSError):
        pass  # The lease will expire and the job becomes claimable again.


def run(
    client: QueueClient,
    extractor: Extractor,
    worker: str,
    *,
    backends: list[str] | None = None,
    lease_seconds: int = 300,
    poll_seconds: float = 10.0,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Process jobs until `should_stop()`; sleep when idle or the app is unreachable."""
    while not should_stop():
        try:
            busy = run_once(
                client, extractor, worker, backends=backends, lease_seconds=lease_seconds
            )
        except (ApiError, OSError):
            busy = False
        if not busy:
            time.sleep(poll_seconds)
