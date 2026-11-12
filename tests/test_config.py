from pathlib import Path

import pytest

from receipt_tracker.config import Settings


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Run each test away from any real `.env` and RECEIPT_TRACKER_ variables."""
    import os

    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("RECEIPT_TRACKER_"):
            monkeypatch.delenv(name)


def test_paths_default_to_subfolders_of_data_dir():
    settings = Settings(data_dir=Path("/srv/rt"))
    assert settings.db_path == Path("/srv/rt/receipt_tracker.db")
    assert settings.watch_dir == Path("/srv/rt/inbox")
    assert settings.archive_dir == Path("/srv/rt/archive")


def test_explicit_paths_are_kept():
    settings = Settings(data_dir=Path("/srv/rt"), watch_dir=Path("/mnt/photos"))
    assert settings.watch_dir == Path("/mnt/photos")
    assert settings.archive_dir == Path("/srv/rt/archive")


def test_environment_variables_are_read(monkeypatch):
    monkeypatch.setenv("RECEIPT_TRACKER_MODEL_SERVER_URL", "http://model-host:11434")
    monkeypatch.setenv("RECEIPT_TRACKER_PORT", "9000")
    settings = Settings()
    assert settings.model_server_url == "http://model-host:11434"
    assert settings.port == 9000


def test_env_file_is_read(tmp_path):
    (tmp_path / ".env").write_text("RECEIPT_TRACKER_DATA_DIR=/from/file\n")
    assert Settings().data_dir == Path("/from/file")
