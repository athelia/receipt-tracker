"""Structured event log: one JSON object per line in a rotating text file.

Each process (app, worker, CLI) writes its own file, `<log_dir>/<process>.log`,
because a rotating file is not safe to share between processes and the worker
may run on another machine. Events carry the job id so one job can be followed
across the files with a grep.

Library code calls `event()`; nothing is written until a process calls
`configure()`. Receipt contents stay out of the log: raw model output is only
written, to its own file, when `log_raw_failures` is enabled.
"""

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from receipt_tracker.config import Settings

ROOT = "receipt_tracker"
RAW_FAILURES_FILE = "raw-failures.jsonl"
_HANDLER_MARK = "_receipt_tracker_handler"

logging.getLogger(ROOT).addHandler(logging.NullHandler())

_current_job: contextvars.ContextVar[int | None] = contextvars.ContextVar("job_id", default=None)


@contextmanager
def job_context(job_id: int) -> Iterator[None]:
    """Events logged inside this block carry `job_id`, even from code that does not know it."""
    token = _current_job.set(job_id)
    try:
        yield
    finally:
        _current_job.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class ConsoleFormatter(logging.Formatter):
    """One readable line per event, for watching a run: `21:03:07 ! name key=value ...`."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        flag = "!" if record.levelno >= logging.WARNING else " "
        fields = getattr(record, "fields", {})
        detail = " ".join(f"{key}={_short(value)}" for key, value in fields.items())
        return f"{stamp} {flag} {record.getMessage()} {detail}".rstrip()


def _short(value: object) -> str:
    text = json.dumps(value, separators=(",", ":"), default=str)
    text = text[1:-1] if text.startswith('"') and text.endswith('"') else text
    return text if len(text) <= 160 else text[:157] + "..."


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Record one event. `fields` become keys of the JSON line (e.g. job_id=3)."""
    job_id = _current_job.get()
    if job_id is not None:
        fields.setdefault("job_id", job_id)
    logging.getLogger(f"{ROOT}.events").log(level, name, extra={"fields": fields})


def configure(settings: Settings, process: str, console: bool | None = None) -> Path:
    """Send this process's events to `<log_dir>/<process>.log`. Safe to call twice.

    With `console` (default: the `log_console` setting) each event is also printed
    as a readable line on stderr.
    """
    assert settings.log_dir is not None
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    path = settings.log_dir / f"{process}.log"
    root = logging.getLogger(ROOT)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        path, maxBytes=settings.log_max_bytes, backupCount=settings.log_backup_count
    )
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    if settings.log_console if console is None else console:
        live = logging.StreamHandler(sys.stderr)
        live.setFormatter(ConsoleFormatter())
        setattr(live, _HANDLER_MARK, True)
        root.addHandler(live)
    root.setLevel(settings.log_level.upper())
    return path


def raw_failure_path(settings: Settings) -> Path | None:
    """Where raw model output of failed extractions goes, or None when disabled."""
    if not settings.log_raw_failures or settings.log_dir is None:
        return None
    return settings.log_dir / RAW_FAILURES_FILE


def write_raw_failure(path: Path, job_id: int, error: str, raw_output: str) -> None:
    """Append a failed extraction's raw model answer. Contains receipt text: local only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "job_id": job_id,
        "error": error,
        "raw_output": raw_output,
    }
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
