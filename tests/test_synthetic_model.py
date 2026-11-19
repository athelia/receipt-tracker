"""Does the real vision model read a receipt we know the answer to?

Skipped unless run with `--run-model`; it needs a reachable Ollama with the
configured model and takes minutes per case on CPU:

    .venv/bin/pytest tests/test_synthetic_model.py --run-model -v

A pass on a clean synthetic render is a regression check, not a measure of
accuracy on real photos; that is what the golden-set evaluation (AA-56) is for.
"""

import pytest

import synthetic
from receipt_tracker.config import get_settings
from receipt_tracker.extractors import create

TRUTH = synthetic.sample_truth()


def test_truth_is_internally_consistent():
    assert sum(line.price_cents for line in TRUTH.lines) == TRUTH.subtotal_cents == 2724
    assert TRUTH.total_cents == 2860
    assert TRUTH.savings_cents == 220
    assert len({(line.code, line.text) for line in TRUTH.lines}) < len(TRUTH.lines)  # has repeats


def test_render_is_a_tall_portrait_image():
    image = synthetic.render(TRUTH)
    assert image.height > image.width


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


@pytest.mark.model
@pytest.mark.parametrize("turn", [0, -90], ids=["upright", "sideways"])
def test_model_reads_a_synthetic_receipt(tmp_path, turn):
    photo = tmp_path / "synthetic.png"
    synthetic.render(TRUTH).rotate(turn, expand=True).save(photo)

    extractor = create("ollama-vision", get_settings())
    result = extractor.extract([photo])

    assert differences(result.receipt) == []
