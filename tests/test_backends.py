"""Backend registry selection and FLIR-unavailable handling (no hardware)."""

import pytest

from octacam.cameras import select_backend
from octacam.cameras.registry import (
    REAL_BACKENDS,
    BackendUnavailable,
    resolve_backend_names,
    teardown_backend,
)


def test_select_unknown_backend_raises():
    with pytest.raises(BackendUnavailable):
        select_backend("nikon")


def test_resolve_backend_names_auto_sweeps_hardware_backends():
    # "auto" (and its aliases / an absent selector) means "every hardware
    # backend", so a rig picks up Basler and FLIR together. fake is never swept.
    assert resolve_backend_names("auto") == list(REAL_BACKENDS)
    assert resolve_backend_names("all") == list(REAL_BACKENDS)
    assert resolve_backend_names(None) == list(REAL_BACKENDS)
    assert resolve_backend_names("  ") == list(REAL_BACKENDS)
    assert "fake" not in resolve_backend_names("auto")


def test_resolve_backend_names_concrete_is_single():
    assert resolve_backend_names("basler") == ["basler"]
    assert resolve_backend_names("FLIR") == ["flir"]
    assert resolve_backend_names("fake") == ["fake"]


def test_select_fake_backend():
    enumerate_fn, factory, extension = select_backend("fake")
    assert extension == "fake"
    assert callable(enumerate_fn) and callable(factory)


def test_flir_module_imports_without_pyspin():
    # The module must import even when PySpin is absent (it is reached only via
    # the registry, which converts the missing SDK to BackendUnavailable).
    import octacam.cameras.flir as flir

    assert flir.FlirBackend.extension == "json"


def test_select_flir_without_pyspin_raises():
    # PySpin ships with the Spinnaker SDK and is not pip-installable, so it is
    # absent in CI; selecting FLIR must surface a clean BackendUnavailable,
    # never a raw ImportError.
    try:
        import PySpin  # type: ignore  # noqa: F401

        pytest.skip("PySpin is installed; cannot test the unavailable path")
    except ImportError:
        pass
    with pytest.raises(BackendUnavailable):
        select_backend("flir")


def test_teardown_backend_noop_for_non_flir():
    teardown_backend("basler")  # must not raise
    teardown_backend("fake")
