from typer.testing import CliRunner

from receipt_tracker import __version__
from receipt_tracker.cli import app
from receipt_tracker.config import get_settings

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("version", "config", "serve"):
        assert command in result.output


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_config_shows_settings_from_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECEIPT_TRACKER_DATA_DIR", str(tmp_path / "store"))
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["config"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0
    assert f"data_dir = {tmp_path / 'store'}" in result.output
