import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-model",
        action="store_true",
        default=False,
        help="Also run tests marked `model`, which call a real local vision model (slow).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "model: calls a real vision model (Ollama); skipped unless --run-model is given"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-model"):
        return
    skip = pytest.mark.skip(reason="needs a real vision model; pass --run-model")
    for item in items:
        if item.get_closest_marker("model"):
            item.add_marker(skip)
