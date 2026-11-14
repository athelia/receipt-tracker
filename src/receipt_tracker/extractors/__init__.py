"""Pluggable receipt extractors. See `base` for the interface."""

from receipt_tracker.extractors import fake
from receipt_tracker.extractors.base import (
    BackendUnavailable,
    ExtractedHeader,
    ExtractedLine,
    ExtractedReceipt,
    ExtractionError,
    ExtractionResult,
    Extractor,
)
from receipt_tracker.extractors.registry import available, create, register

register("fake", fake.build, image_destination="none")

__all__ = [
    "BackendUnavailable",
    "ExtractedHeader",
    "ExtractedLine",
    "ExtractedReceipt",
    "ExtractionError",
    "ExtractionResult",
    "Extractor",
    "available",
    "create",
    "register",
]
