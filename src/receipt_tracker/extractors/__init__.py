"""Pluggable receipt extractors. See `base` for the interface."""

from receipt_tracker.extractors import fake, ollama_vision
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
register("ollama-vision", ollama_vision.build, image_destination="local_network")

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
