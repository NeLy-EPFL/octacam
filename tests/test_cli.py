"""CLI smoke tests for the typer app (no real recording is started)."""

import os

os.environ.setdefault("PYLON_CAMEMU", "2")

from typer.testing import CliRunner

import octacam
from octacam.cli import _resolve_enabled, app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert octacam.__version__ in result.output


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("serve", "list-cameras", "record", "transcode"):
        assert command in result.output


def test_no_args_prints_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_list_cameras_emits_tab_separated_lines():
    result = runner.invoke(app, ["list-cameras"])
    assert result.exit_code == 0
    # PYLON_CAMEMU=2 guarantees the emulated cameras show up.
    assert "0815-0000" in result.output
    emulated = [line for line in result.output.splitlines() if "0815-" in line]
    assert emulated and all("\t" in line for line in emulated)


def test_record_help_shows_enum_choices():
    result = runner.invoke(app, ["record", "--help"])
    assert result.exit_code == 0
    assert "[x264|raw|mjpg|h264]" in result.output
    assert "[software|hardware]" in result.output


def test_invalid_log_level_rejected():
    result = runner.invoke(app, ["--log-level", "bogus", "list-cameras"])
    assert result.exit_code != 0


def test_serve_rejects_missing_config_dir():
    result = runner.invoke(app, ["serve", "/no/such/dir"])
    assert result.exit_code != 0


def test_transcode_requires_paths():
    result = runner.invoke(app, ["transcode"])
    assert result.exit_code != 0


def test_resolve_enabled():
    # None / empty -> no override (use the config).
    assert _resolve_enabled(None, False) is None
    assert _resolve_enabled([], False) is None
    # Explicit plugin names are passed through.
    assert _resolve_enabled(["arduino"], False) == ["arduino"]
    # --no-plugins wins and disables everything.
    assert _resolve_enabled(["arduino"], True) == []
    assert _resolve_enabled(None, True) == []
