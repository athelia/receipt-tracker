"""The ground truth: golden receipts from the database, paired with their photos.

The workbook was entered by hand and imported with origin `golden`. A manifest
CSV says which photo shows which receipt (store, date, receipt number), so a
backend can be run over the photos and its answer compared with the entered lines.
"""

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TruthLine:
    item_code: str | None
    name: str
    quantity_milli: int | None
    unit_price_cents: int | None
    discount_bp: int | None
    line_price_cents: int


@dataclass(frozen=True)
class TruthReceipt:
    receipt_id: int
    store: str
    date: str  # YYYY-MM-DD; the workbook has no time of day
    number: str  # empty when the receipt has none
    lines: tuple[TruthLine, ...]

    @property
    def key(self) -> str:
        """A stable name for this receipt in stored results."""
        return f"{self.date}_{self.store}_{self.number}".replace(" ", "-")


@dataclass(frozen=True)
class Case:
    """One receipt to read: its photos (in order) and what they should say."""

    truth: TruthReceipt
    photos: tuple[Path, ...]


@dataclass(frozen=True)
class Cases:
    cases: tuple[Case, ...]
    skipped: tuple[str, ...]  # photos that could not be paired, each with the reason


def load_truth(conn: sqlite3.Connection) -> list[TruthReceipt]:
    """Every golden receipt with its lines in printed order."""
    receipts = conn.execute(
        "SELECT r.id, s.name, r.purchased_at, r.receipt_number FROM receipts r "
        "JOIN stores s ON s.id = r.store_id WHERE r.origin = 'golden' ORDER BY r.id"
    ).fetchall()
    result = []
    for row in receipts:
        lines = conn.execute(
            "SELECT item_code, raw_text, quantity_milli, unit_price_cents, discount_bp, "
            "line_price_cents FROM line_items WHERE receipt_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        result.append(
            TruthReceipt(
                receipt_id=row["id"],
                store=row["name"],
                date=(row["purchased_at"] or "")[:10],
                number=_number(row["receipt_number"]),
                lines=tuple(TruthLine(*tuple(line)) for line in lines),
            )
        )
    return result


def _number(value: object) -> str:
    return "" if value is None else str(value).strip()


def pair_photos(manifest: Path, truth: list[TruthReceipt]) -> Cases:
    """Pair manifest photos with golden receipts; say why any photo is left out.

    Photo paths in the manifest are relative to the manifest's folder. A receipt with
    several photos gets them in sequence order.
    """
    by_key = {(t.store, t.date, t.number): t for t in truth}
    by_day: dict[tuple[str, str], list[TruthReceipt]] = {}
    for receipt in truth:
        by_day.setdefault((receipt.store, receipt.date), []).append(receipt)

    photos: dict[int, list[tuple[int, Path]]] = {}
    skipped = []
    with manifest.open(newline="") as handle:
        for row in csv.DictReader(handle):
            path = manifest.parent / row["photo"]
            if not row["store"]:
                skipped.append(f"{row['photo']}: not in the workbook")
                continue
            if not path.exists():
                skipped.append(f"{row['photo']}: file is missing")
                continue
            key = (row["store"], row["purchased_at"], _number(row["receipt_number"]))
            receipt = by_key.get(key)
            if receipt is None:
                same_day = by_day.get(key[:2], [])
                receipt = same_day[0] if len(same_day) == 1 else None
            if receipt is None:
                skipped.append(f"{row['photo']}: no single golden receipt matches")
                continue
            photos.setdefault(receipt.receipt_id, []).append((int(row["sequence"]), path))

    cases = tuple(
        Case(receipt, tuple(path for _, path in sorted(photos[receipt.receipt_id])))
        for receipt in truth
        if receipt.receipt_id in photos
    )
    return Cases(cases, tuple(skipped))
