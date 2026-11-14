"""Store an extraction's structured result.

Each run gets its own `extractions` row and its own `line_items`, so
re-running extraction keeps the earlier results for comparison. The receipt's
header fields follow the latest run, except for a receipt a person has already
accepted: that one is never overwritten.
"""

import sqlite3

from receipt_tracker.extractors import ExtractedReceipt


def save_receipt(
    conn: sqlite3.Connection, extraction_id: int, receipt_id: int, extracted: ExtractedReceipt
) -> None:
    """Write line items and header fields. Call inside the caller's transaction."""
    for position, line in enumerate(extracted.lines, start=1):
        conn.execute(
            """
            INSERT INTO line_items (
                receipt_id, extraction_id, position, item_code, raw_text, quantity_milli,
                unit, unit_price_cents, discount_bp, line_price_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                extraction_id,
                position,
                line.item_code,
                line.raw_text,
                line.quantity_milli,
                line.unit,
                line.unit_price_cents,
                line.discount_bp,
                line.line_price_cents,
            ),
        )
    _update_header(conn, receipt_id, extracted)


def _update_header(conn: sqlite3.Connection, receipt_id: int, extracted: ExtractedReceipt) -> None:
    header = extracted.header
    store_id = None
    if header.store_name:
        conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (header.store_name,))
        store_id = conn.execute(
            "SELECT id FROM stores WHERE name = ?", (header.store_name,)
        ).fetchone()[0]
    conn.execute(
        """
        UPDATE receipts SET
            store_id = COALESCE(?, store_id),
            purchased_at = COALESCE(?, purchased_at),
            receipt_number = COALESCE(?, receipt_number),
            subtotal_cents = COALESCE(?, subtotal_cents),
            total_cents = COALESCE(?, total_cents),
            payment_method = COALESCE(?, payment_method)
        WHERE id = ? AND review_status != 'accepted'
        """,
        (
            store_id,
            header.purchased_at,
            header.receipt_number,
            header.subtotal_cents,
            header.total_cents,
            header.payment_method,
            receipt_id,
        ),
    )


def extractions_for(conn: sqlite3.Connection, receipt_id: int) -> list[int]:
    """Extraction ids for a receipt, oldest first."""
    rows = conn.execute(
        "SELECT id FROM extractions WHERE receipt_id = ? ORDER BY id", (receipt_id,)
    )
    return [row["id"] for row in rows]
