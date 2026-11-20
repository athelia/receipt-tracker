"""Score one extracted receipt against its ground truth.

Everything here is a pure function over plain values, so it is tested with
hand-made examples. A score is a dict of *counts* (hits and totals), never a
ratio, so scores add up across receipts and a ratio is taken only at the end:
that way a receipt with 60 lines weighs more than one with 3.

The ground truth has what was typed into the workbook: store, date, receipt
number, and per line the item code, name, quantity, unit price, discount and
price. It has no subtotal, total, payment method or savings figure, so those are
not scored against truth; the consistency checks use them instead.
"""

import re
from collections.abc import Sequence

from receipt_tracker.evaluation.truth import TruthLine, TruthReceipt
from receipt_tracker.extractors.base import ExtractedLine, ExtractedReceipt

# Points for how well an extracted line fits a truth line. A pair counts as the same
# line when it scores at least MATCH_SCORE: an exact code or exact price on its own is
# enough, and so is a near-miss code with a similar description.
CODE_EXACT = 3
PRICE_EXACT = 3
CODE_NEAR = 1  # a code within CODE_NEAR_CER, so a misread digit still lines up
TEXT_SIMILAR = 1
CODE_NEAR_CER = 0.25
TEXT_SIMILAR_CER = 0.5
MATCH_SCORE = 2

COUNT_FIELDS = (
    "truth_lines",
    "extracted_lines",
    "matched_lines",
    "price_hits",
    "code_truth",
    "code_extracted",
    "code_hits",
    "code_edits",
    "code_chars",
    "text_edits",
    "text_chars",
    "quantity_truth",
    "quantity_hits",
    "unit_price_truth",
    "unit_price_hits",
    "discount_truth",
    "discount_hits",
    "store_truth",
    "store_hits",
    "date_truth",
    "date_hits",
    "number_truth",
    "number_hits",
    "sum_checked",
    "sum_passed",
    "savings_checked",
    "savings_passed",
)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: single-character insertions, deletions and substitutions."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def char_error_rate(truth: str, read: str) -> float:
    """Edit distance as a share of the true length (0 is perfect; may exceed 1)."""
    return edit_distance(truth, read) / max(1, len(truth))


def normalize_code(code: str | None) -> str:
    """Codes compare without spaces or case."""
    return re.sub(r"\s+", "", code or "").upper()


def normalize_text(text: str | None) -> str:
    """Descriptions compare without case, punctuation or runs of spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def normalize_store(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def pair_score(read: ExtractedLine, truth: TruthLine) -> int:
    """How well an extracted line fits a truth line, 0 when it does not."""
    score = 0
    read_code, true_code = normalize_code(read.item_code), normalize_code(truth.item_code)
    if read_code and true_code:
        if read_code == true_code:
            score += CODE_EXACT
        elif char_error_rate(true_code, read_code) <= CODE_NEAR_CER:
            score += CODE_NEAR
    if read.line_price_cents == truth.line_price_cents:
        score += PRICE_EXACT
    read_text, true_text = normalize_text(read.raw_text), normalize_text(truth.name)
    if read_text and true_text and char_error_rate(true_text, read_text) <= TEXT_SIMILAR_CER:
        score += TEXT_SIMILAR
    return score


def align_lines(
    read: Sequence[ExtractedLine], truth: Sequence[TruthLine]
) -> list[tuple[ExtractedLine, TruthLine]]:
    """Pair extracted lines with truth lines, keeping the printed order.

    A receipt is read top to bottom, so lines are lined up like two strings (a
    best-scoring alignment, skipping a line on either side). This copes with
    repeated identical lines, which a lookup by price or code cannot. Pairs that
    score below MATCH_SCORE are not matches and are left out.
    """
    rows, cols = len(read), len(truth)
    scores = [[pair_score(r, t) for t in truth] for r in read]
    best = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            take = scores[i][j] + best[i + 1][j + 1] if scores[i][j] >= MATCH_SCORE else -1
            best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < rows and j < cols:
        if scores[i][j] >= MATCH_SCORE and best[i][j] == scores[i][j] + best[i + 1][j + 1]:
            pairs.append((read[i], truth[j]))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def implied_savings_cents(lines: Sequence[ExtractedLine]) -> int:
    """What the percentage discounts on the lines add up to.

    The printed price is already net, so the discount is price * bp / (10000 - bp).
    """
    return sum(
        round(line.line_price_cents * line.discount_bp / (10000 - line.discount_bp))
        for line in lines
        if line.discount_bp and line.discount_bp < 10000
    )


def score_receipt(read: ExtractedReceipt | None, truth: TruthReceipt) -> dict[str, int]:
    """The counts for one receipt. `read` is None when extraction failed."""
    counts = dict.fromkeys(COUNT_FIELDS, 0)
    counts["truth_lines"] = len(truth.lines)
    counts["code_truth"] = sum(1 for line in truth.lines if normalize_code(line.item_code))
    counts["quantity_truth"] = sum(1 for line in truth.lines if line.quantity_milli is not None)
    counts["unit_price_truth"] = sum(1 for line in truth.lines if line.unit_price_cents is not None)
    counts["discount_truth"] = sum(1 for line in truth.lines if line.discount_bp is not None)
    # The truth side of the header counts even when extraction failed, so a failure is a miss.
    counts["store_truth"] = 1
    counts["date_truth"] = 1 if truth.date else 0
    counts["number_truth"] = 1 if truth.number else 0
    if read is None:
        return counts

    counts["extracted_lines"] = len(read.lines)
    counts["code_extracted"] = sum(1 for line in read.lines if normalize_code(line.item_code))
    for got, want in align_lines(read.lines, truth.lines):
        counts["matched_lines"] += 1
        counts["price_hits"] += got.line_price_cents == want.line_price_cents
        got_code, want_code = normalize_code(got.item_code), normalize_code(want.item_code)
        if want_code:
            counts["code_hits"] += got_code == want_code
            counts["code_edits"] += edit_distance(want_code, got_code)
            counts["code_chars"] += len(want_code)
        want_text = normalize_text(want.name)
        counts["text_edits"] += edit_distance(want_text, normalize_text(got.raw_text))
        counts["text_chars"] += len(want_text)
        if want.quantity_milli is not None:
            counts["quantity_hits"] += got.quantity_milli == want.quantity_milli
        if want.unit_price_cents is not None:
            counts["unit_price_hits"] += got.unit_price_cents == want.unit_price_cents
        if want.discount_bp is not None:
            counts["discount_hits"] += got.discount_bp == want.discount_bp

    header = read.header
    counts["store_hits"] = normalize_store(header.store_name) == normalize_store(truth.store)
    counts["date_hits"] = bool(truth.date) and (header.purchased_at or "")[:10] == truth.date
    counts["number_hits"] = bool(truth.number) and normalize_code(
        header.receipt_number
    ) == normalize_code(truth.number)

    if header.subtotal_cents is not None:
        counts["sum_checked"] = 1
        counts["sum_passed"] = sum(line.line_price_cents for line in read.lines) == (
            header.subtotal_cents
        )
    if header.savings_cents is not None:
        counts["savings_checked"] = 1
        discounted = sum(1 for line in read.lines if line.discount_bp)
        allowed = max(1, discounted)  # one cent of rounding per discounted line
        counts["savings_passed"] = (
            abs(implied_savings_cents(read.lines) - header.savings_cents) <= allowed
        )
    return {name: int(value) for name, value in counts.items()}


def add_counts(*scores: dict[str, int]) -> dict[str, int]:
    """Sum scores field by field."""
    return {name: sum(score.get(name, 0) for score in scores) for name in COUNT_FIELDS}


def ratio(hits: int, total: int) -> float | None:
    """A share, or None when there was nothing to measure (so it is shown as n/a)."""
    return hits / total if total else None


def metrics(counts: dict[str, int]) -> dict[str, float | None]:
    """The reported figures, derived from summed counts."""
    return {
        "line_recall": ratio(counts["matched_lines"], counts["truth_lines"]),
        "line_precision": ratio(counts["matched_lines"], counts["extracted_lines"]),
        "price_recall": ratio(counts["price_hits"], counts["truth_lines"]),
        "price_precision": ratio(counts["price_hits"], counts["extracted_lines"]),
        "code_recall": ratio(counts["code_hits"], counts["code_truth"]),
        "code_precision": ratio(counts["code_hits"], counts["code_extracted"]),
        "code_cer": ratio(counts["code_edits"], counts["code_chars"]),
        "text_cer": ratio(counts["text_edits"], counts["text_chars"]),
        "quantity_match": ratio(counts["quantity_hits"], counts["quantity_truth"]),
        "unit_price_match": ratio(counts["unit_price_hits"], counts["unit_price_truth"]),
        "discount_match": ratio(counts["discount_hits"], counts["discount_truth"]),
        "store_match": ratio(counts["store_hits"], counts["store_truth"]),
        "date_match": ratio(counts["date_hits"], counts["date_truth"]),
        "number_match": ratio(counts["number_hits"], counts["number_truth"]),
        "sum_check_pass": ratio(counts["sum_passed"], counts["sum_checked"]),
        "savings_check_pass": ratio(counts["savings_passed"], counts["savings_checked"]),
    }
