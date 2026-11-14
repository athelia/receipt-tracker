"""The extractor interface: photos in, a structured receipt out.

Backends turn receipt photos into an `ExtractedReceipt` and report which
backend, model and model version produced it, plus the raw model output, so
results can be compared across backends and re-run.

Hard constraint: no backend may send images off the home network. A backend
declares where it sends images when it is registered, and the registry refuses
anything other than "none" (the images stay in this process) or
"local_network" (e.g. a model server on the LAN).
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Protocol

ImageDestination = Literal["none", "local_network"]


class BackendUnavailable(Exception):
    """The backend's model server could not be reached. The job is retried."""


class ExtractionError(Exception):
    """The backend ran but could not produce a usable result for this receipt."""


def _check_confidence(value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {value}")


def _check_int(name: str, value: object, *, required: bool = False) -> None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")


@dataclass(frozen=True)
class ExtractedLine:
    """One printed line. Only the printed line price is required."""

    raw_text: str
    line_price_cents: int
    item_code: str | None = None
    quantity_milli: int | None = None
    unit: str | None = None
    unit_price_cents: int | None = None
    discount_bp: int | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        _check_int("line_price_cents", self.line_price_cents, required=True)
        for name in ("quantity_milli", "unit_price_cents", "discount_bp"):
            _check_int(name, getattr(self, name))
        if self.discount_bp is not None and not 0 <= self.discount_bp <= 10000:
            raise ValueError("discount_bp must be between 0 and 10000")
        _check_confidence(self.confidence)


@dataclass(frozen=True)
class ExtractedHeader:
    """Receipt-level fields. Anything the backend could not read stays None."""

    store_name: str | None = None
    purchased_at: str | None = None
    receipt_number: str | None = None
    subtotal_cents: int | None = None
    total_cents: int | None = None
    payment_method: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        _check_int("subtotal_cents", self.subtotal_cents)
        _check_int("total_cents", self.total_cents)
        _check_confidence(self.confidence)


@dataclass(frozen=True)
class ExtractedReceipt:
    header: ExtractedHeader = field(default_factory=ExtractedHeader)
    lines: tuple[ExtractedLine, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractedReceipt":
        """Rebuild from `to_dict` output (used when a worker posts a result)."""
        return cls(
            header=ExtractedHeader(**data.get("header", {})),
            lines=tuple(ExtractedLine(**line) for line in data.get("lines", [])),
        )


@dataclass(frozen=True)
class ExtractionResult:
    """What a backend returns, with the provenance that gets stored."""

    receipt: ExtractedReceipt
    backend: str
    model: str
    model_version: str
    raw_output: str | None = None

    def __post_init__(self) -> None:
        for name in ("backend", "model", "model_version"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")


class Extractor(Protocol):
    """A configured backend. Implementations are registered by name."""

    def extract(self, photos: list[Path]) -> ExtractionResult:
        """Read the receipt in `photos` (one or more, in order).

        Raise `BackendUnavailable` if the model server cannot be reached and
        `ExtractionError` if the receipt could not be read.
        """
        ...
