"""Units of measure as they appear on receipts and in hand-entered data."""

# Spellings that mean the same unit. Extended by unit normalisation work.
_ALIASES = {
    "lbs": "lb",
    "bch": "bunch",
}


def canonical_unit(raw: str | None) -> str | None:
    """Lower-case a unit and fold known alternate spellings. Empty becomes None."""
    if raw is None:
        return None
    unit = raw.strip().lower()
    if not unit:
        return None
    return _ALIASES.get(unit, unit)
