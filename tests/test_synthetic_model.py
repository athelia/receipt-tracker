"""Does the real vision model read a receipt we know the answer to?

Skipped unless run with `--run-model`; it needs a reachable Ollama with the
configured model and takes minutes per case on CPU:

    .venv/bin/pytest tests/test_synthetic_model.py --run-model -v

A pass on a clean synthetic render is a regression check, not a measure of
accuracy on real photos; that is what the golden-set evaluation (AA-56) is for.

Known gap, deliberately not asserted: the model does not reliably set
`discount_percent` from a "20% off" note. The printed price is already net and
the receipt-level savings figure is checked, so nothing downstream depends on it.
"""

import pytest

import synthetic
from receipt_tracker.config import get_settings
from receipt_tracker.extractors import create

TRUTH = synthetic.sample_truth()


def test_truth_is_internally_consistent():
    assert sum(line.price_cents for line in TRUTH.lines) == TRUTH.subtotal_cents == 2818
    assert TRUTH.total_cents == 2954
    assert TRUTH.savings_cents == 315
    assert len({(line.code, line.text) for line in TRUTH.lines}) < len(TRUTH.lines)  # has repeats


def differences(receipt):
    """Everything the model got wrong, as readable strings. Empty means a perfect read."""
    problems = []
    header = receipt.header
    for name, expected in [
        ("subtotal_cents", TRUTH.subtotal_cents),
        ("total_cents", TRUTH.total_cents),
        ("savings_cents", TRUTH.savings_cents),
        ("receipt_number", TRUTH.number),
        ("payment_method", TRUTH.payment),
    ]:
        if getattr(header, name) != expected:
            problems.append(f"{name}: expected {expected!r}, got {getattr(header, name)!r}")
    expected_lines = [(line.code, line.price_cents) for line in TRUTH.lines]
    got_lines = [(line.item_code, line.line_price_cents) for line in receipt.lines]
    if got_lines != expected_lines:
        problems.append(f"lines: expected {expected_lines}, got {got_lines}")
    return problems


CASES = {
    "upright": dict(turn=0),
    "sideways": dict(turn=-90),
    "on a table": dict(turn=0, table=dict(receipt_width=0.45, angle=5)),
    "sideways on a table": dict(turn=-90, table=dict(receipt_width=0.45, angle=5)),
}


@pytest.mark.model
@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_model_reads_a_synthetic_receipt(tmp_path, case):
    image = synthetic.render(TRUTH)
    if "table" in case:
        image, *_ = synthetic.on_table(image, **case["table"])
    photo = tmp_path / "synthetic.png"
    image.rotate(case["turn"], expand=True).save(photo)

    extractor = create("ollama-vision", get_settings())
    result = extractor.extract([photo])

    assert differences(result.receipt) == []
