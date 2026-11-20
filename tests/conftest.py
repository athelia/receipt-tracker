import json
import logging

import pytest

from receipt_tracker import logging_setup
from receipt_tracker.config import Settings


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


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, _env_file=None)


@pytest.fixture
def start_logging(settings):
    """Call it to configure logging for the test; the handlers are removed afterwards."""
    yield lambda **options: logging_setup.configure(settings, "test", **options)
    root = logging.getLogger(logging_setup.ROOT)
    for handler in list(root.handlers):
        if getattr(handler, logging_setup._HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture
def log(start_logging):
    """Route events to a file under tmp_path and return a reader for them."""
    path = start_logging()
    return lambda: [json.loads(line) for line in path.read_text().splitlines()]
