"""Move new photos from the watched folder into the archive and queue them for extraction.

The watched folder is polled rather than subscribed to: file-change notifications are
unreliable on network shares and container mounts, and a poll every few seconds is cheap.

Each photo is copied into the archive, recorded in the database, and only then removed
from the watched folder, so an interruption at any point is repaired by the next scan.
"""

import hashlib
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})

# `name-1.jpg`, `name-2.jpg`: photos of one long receipt, in order.
_PART = re.compile(r"^(?P<key>.+)-(?P<number>[1-9])$")


@dataclass
class Ingested:
    name: str
    receipt_id: int
    sequence: int
    joined: bool  # added to a receipt that an earlier photo created


@dataclass
class IngestReport:
    ingested: list[Ingested] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def changes(self) -> list[str]:
        """Lines for files that were moved out of the watched folder."""
        lines = []
        for item in self.ingested:
            if item.joined:
                lines.append(
                    f"{item.name}: added to receipt {item.receipt_id} as photo {item.sequence}"
                )
            else:
                lines.append(f"{item.name}: receipt {item.receipt_id}, queued for extraction")
        lines += [
            f"{name}: already archived, removed from the watched folder" for name in self.duplicates
        ]
        return lines

    def left_in_place(self) -> list[str]:
        """Lines for files that are still in the watched folder."""
        lines = [f"{name}: still being written, will retry" for name in self.waiting]
        lines += [f"{name}: left in place, {reason}" for name, reason in self.skipped]
        return lines

    def lines(self) -> list[str]:
        return self.changes() + self.left_in_place()


def scan(
    conn: sqlite3.Connection,
    watch_dir: Path,
    archive_dir: Path,
    *,
    settle_seconds: float = 5.0,
    now: float | None = None,
) -> IngestReport:
    """Ingest every ready photo in `watch_dir`. Subfolders and hidden files are ignored.

    A file is ready once it has not been modified for `settle_seconds`, so a photo that
    is still being copied in is left for the next scan.
    """
    report = IngestReport()
    watch_dir.mkdir(parents=True, exist_ok=True)
    current = time.time() if now is None else now
    for path in sorted(watch_dir.iterdir(), key=lambda p: p.name.lower()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            report.skipped.append((path.name, "not a supported image type"))
            continue
        try:
            stat = path.stat()
            if stat.st_size == 0 or current - stat.st_mtime < settle_seconds:
                report.waiting.append(path.name)
            elif not _looks_like_image(path):
                report.skipped.append((path.name, "the content is not a recognised image"))
            else:
                _ingest_one(conn, path, archive_dir, current, report)
        except OSError as error:
            report.skipped.append((path.name, f"could not be read or moved ({error.strerror})"))
    return report


def watch(
    scan_once: Callable[[], IngestReport],
    emit: Callable[[str], None],
    *,
    interval: float,
    max_scans: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Scan repeatedly. A file left in place is reported once, not on every scan."""
    announced: set[str] = set()
    scans = 0
    while True:
        report = scan_once()
        for line in report.changes():
            emit(line)
        left = report.left_in_place()
        for line in left:
            if line not in announced:
                emit(line)
        announced = set(left)
        scans += 1
        if max_scans is not None and scans >= max_scans:
            return
        sleep(interval)


def _ingest_one(
    conn: sqlite3.Connection, path: Path, archive_dir: Path, current: float, report: IngestReport
) -> None:
    digest = _sha256(path)
    existing = conn.execute(
        "SELECT archive_path, deleted_at FROM photos WHERE sha256 = ?", (digest,)
    ).fetchone()
    if existing is not None:
        # Put the archived copy back if it went missing other than by retention.
        if existing["deleted_at"] is None and not (archive_dir / existing["archive_path"]).exists():
            if not _store(path, archive_dir / existing["archive_path"], digest):
                report.waiting.append(path.name)
                return
        path.unlink()
        report.duplicates.append(path.name)
        return

    received = datetime.fromtimestamp(current, UTC)
    relative = f"{received:%Y}/{received:%m}/{digest}{path.suffix.lower()}"
    if not _store(path, archive_dir / relative, digest):
        report.waiting.append(path.name)
        return

    key, number = _split_part(path.stem)
    sequence = number or 1
    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt_id = _sibling_receipt(conn, key, number)
        joined = receipt_id is not None
        if receipt_id is None:
            receipt_id = conn.execute(
                "INSERT INTO receipts (origin) VALUES ('extracted')"
            ).lastrowid
            conn.execute("INSERT INTO jobs (receipt_id) VALUES (?)", (receipt_id,))
        conn.execute(
            "INSERT INTO photos (receipt_id, sequence, sha256, original_filename,"
            " archive_path, byte_size, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                sequence,
                digest,
                path.name,
                relative,
                path.stat().st_size,
                received.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        # Another scanner archived the same photo between our check and our insert.
        conn.execute("ROLLBACK")
        path.unlink()
        report.duplicates.append(path.name)
        return
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    path.unlink()
    report.ingested.append(Ingested(path.name, receipt_id, sequence, joined))


def _sibling_receipt(conn: sqlite3.Connection, key: str, number: int | None) -> int | None:
    """The receipt an earlier part of the same multi-photo receipt created, if it is
    still waiting for extraction. Once extraction has started, a late part becomes its
    own receipt."""
    if number is None:
        return None
    rows = conn.execute(
        "SELECT p.receipt_id, p.original_filename FROM photos p"
        " JOIN jobs j ON j.receipt_id = p.receipt_id"
        " WHERE j.state = 'queued' ORDER BY p.id DESC"
    ).fetchall()
    for row in rows:
        other_key, other_number = _split_part(Path(row["original_filename"]).stem)
        if other_number is None or other_key != key:
            continue
        taken = conn.execute(
            "SELECT 1 FROM photos WHERE receipt_id = ? AND sequence = ?",
            (row["receipt_id"], number),
        ).fetchone()
        if taken is None:
            return row["receipt_id"]
    return None


def _split_part(stem: str) -> tuple[str, int | None]:
    match = _PART.match(stem)
    if match is None:
        return stem.casefold(), None
    return match["key"].casefold(), int(match["number"])


def _store(source: Path, destination: Path, digest: str) -> bool:
    """Copy `source` to `destination` atomically. False if the file changed meanwhile."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    shutil.copyfile(source, partial)
    if _sha256(partial) != digest:
        partial.unlink()
        return False
    os.replace(partial, destination)
    return True


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _looks_like_image(path: Path) -> bool:
    """Check the file's leading bytes against its extension's format."""
    with path.open("rb") as handle:
        head = handle.read(12)
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return head[4:8] == b"ftyp"  # HEIC / HEIF
