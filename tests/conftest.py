"""Suite harness: emulated cameras, a per-test cache, a clean logger, hardware guards.

The environment defaults are set at import, before any test module imports
octacam: pylon's emulator and the fake backend each provide two cameras, and the
update check never reaches PyPI.
"""

import logging
import os
from pathlib import Path

import pytest

os.environ.setdefault("PYLON_CAMEMU", "2")
os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")
os.environ["OCTACAM_NO_UPDATE_CHECK"] = "1"


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Collect the browser tests only when the command line names their file.

    pytest asks this only about paths it reached by walking a directory. The
    browser tests stay out of a plain run because playwright's sync API leaves an
    event loop running, which breaks every later ``asyncio.run``."""
    return collection_path.name == "test_frontend.py" or None


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    """The recording cache, per test: nothing touches ``~/.cache/octacam``."""
    path = tmp_path / "cache"
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def _restore_octacam_logger():
    """Undo ``cli._setup_logging``, which swaps the process-wide octacam logger's
    handlers and stops propagation, so ``caplog`` sees every later test's records."""
    logger = logging.getLogger("octacam")
    level, handlers, propagate = logger.level, logger.handlers[:], logger.propagate
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers
    logger.propagate = propagate


@pytest.fixture(autouse=True)
def no_flash(monkeypatch):
    """No test runs arduino-cli: discovery finds a fake one, and a flash succeeds
    without running it. A module that tests the real code overrides this fixture."""
    from octacam import firmware

    def flash(spec, port, needed_build, **kwargs):
        return firmware.FlashResult(
            True,
            f"uploaded build {needed_build} to {port}",
            "compiled\nuploaded",
            build=needed_build,
        )

    monkeypatch.setattr(firmware, "arduino_cli_path", lambda: "/fake/arduino-cli")
    monkeypatch.setattr(firmware, "flash", flash)


@pytest.fixture(autouse=True)
def no_usb_reset(monkeypatch):
    """No test resets a USB device or waits for one to come back. A test of the
    recovery path patches its own outcome; a module that tests the real code
    overrides this fixture."""
    from octacam import serial_ports

    monkeypatch.setattr(
        serial_ports, "reset_usb_device", lambda device: (False, "test: suppressed")
    )
    monkeypatch.setattr(
        serial_ports, "wait_for_device", lambda device, timeout=3.0: True
    )
