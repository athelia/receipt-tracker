"""Backend registry: backends are selected by name from configuration."""

from collections.abc import Callable

from receipt_tracker.config import Settings
from receipt_tracker.extractors.base import Extractor, ImageDestination

_ALLOWED_DESTINATIONS = {"none", "local_network"}
_factories: dict[str, Callable[[Settings], Extractor]] = {}


def register(
    name: str,
    factory: Callable[[Settings], Extractor],
    *,
    image_destination: ImageDestination,
) -> None:
    """Add a backend. `factory` builds it from settings.

    `image_destination` says where the backend sends photos: "none" or
    "local_network". Anything else is refused.
    """
    if name in _factories:
        raise ValueError(f"Extractor backend already registered: {name}")
    if image_destination not in _ALLOWED_DESTINATIONS:
        raise ValueError(
            f"Backend {name!r}: image_destination must be one of "
            f"{sorted(_ALLOWED_DESTINATIONS)}; images may not leave the home network"
        )
    _factories[name] = factory


def create(name: str, settings: Settings) -> Extractor:
    try:
        factory = _factories[name]
    except KeyError:
        known = ", ".join(sorted(_factories)) or "none"
        raise ValueError(f"Unknown extractor backend {name!r}. Available: {known}") from None
    return factory(settings)


def available() -> list[str]:
    return sorted(_factories)
