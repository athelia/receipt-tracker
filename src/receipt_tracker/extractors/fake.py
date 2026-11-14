"""A fake backend for tests and manual runs. It never reads the photos."""

from pathlib import Path

from receipt_tracker.config import Settings
from receipt_tracker.extractors.base import (
    ExtractedHeader,
    ExtractedLine,
    ExtractedReceipt,
    ExtractionResult,
)


class FakeExtractor:
    """Returns a canned receipt, or the one it was given."""

    def __init__(self, receipt: ExtractedReceipt | None = None, model_version: str = "1"):
        self.receipt = receipt or ExtractedReceipt(
            header=ExtractedHeader(store_name="Fake Store", total_cents=300),
            lines=(
                ExtractedLine("MILK", 200, item_code="111", confidence=0.9),
                ExtractedLine("BREAD", 100, item_code="222", confidence=0.8),
            ),
        )
        self.model_version = model_version
        self.calls: list[list[Path]] = []

    def extract(self, photos: list[Path]) -> ExtractionResult:
        self.calls.append(list(photos))
        return ExtractionResult(
            receipt=self.receipt,
            backend="fake",
            model="fake-model",
            model_version=self.model_version,
            raw_output=f"fake output for {len(photos)} photo(s)",
        )


def build(settings: Settings) -> FakeExtractor:
    return FakeExtractor()
