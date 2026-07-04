"""Backend registry selection, the auto cascade, and unavailable handling.

Pure Python, no hardware: the vendor tiers may or may not be importable here, so
these assert the cascade *structure* and the missing-SDK → BackendUnavailable
contract rather than any particular camera being present.
"""

import pytest

from octacam.cameras import select_backend
from octacam.cameras.registry import (
    BACKENDS,
    CASCADE,
    BackendUnavailable,
    available_backends,
    resolve_backend_names,
    teardown_backend,
)


def test_backends_and_cascade_membership():
    # The cascade is the auto-selected tiers, in preference order; fake is listed
    # in BACKENDS but never part of the auto cascade.
    assert CASCADE == ("basler", "flir", "spinnaker", "pycameleon")
    assert "fake" in BACKENDS and "fake" not in CASCADE
    # pycameleon is a core dep, so it is the guaranteed floor of the cascade.
    assert CASCADE[-1] == "pycameleon"
    # spinnaker (the Spinnaker C API via ctypes) sits at the FLIR-vendor position,
    # just below flir, so it claims the FLIRs on modern Python where PySpin drops.
    assert CASCADE.index("spinnaker") == CASCADE.index("flir") + 1
    assert "spinnaker" in BACKENDS
    # harvesters is DELIBERATELY excluded from the auto cascade: the only
    # freely-installable GenTL producer (Balluff mvIMPACT) watermarks frames after
    # an ~8 s eval window, so it must never be auto-selected — only opted into by
    # name. It stays a known backend and remains selectable explicitly.
    assert "harvesters" not in CASCADE
    assert "harvesters" in BACKENDS
    assert resolve_backend_names("harvesters") == ["harvesters"]


def test_select_unknown_backend_raises():
    with pytest.raises(BackendUnavailable):
        select_backend("nikon")


def test_resolve_backend_names_auto_is_available_cascade():
    # "auto" (and its aliases / an absent selector) resolves to the available
    # cascade tiers in priority order — never fake.
    available = available_backends()
    assert resolve_backend_names("auto") == available
    assert resolve_backend_names("all") == available
    assert resolve_backend_names(None) == available
    assert resolve_backend_names("  ") == available
    assert "fake" not in resolve_backend_names("auto")
    # Whatever is available is a sub-sequence of the cascade, in cascade order,
    # and pycameleon (core) is always present.
    assert available == [b for b in CASCADE if b in available]
    assert "pycameleon" in available


def test_resolve_backend_names_concrete_is_single():
    assert resolve_backend_names("basler") == ["basler"]
    assert resolve_backend_names("FLIR") == ["flir"]
    assert resolve_backend_names("spinnaker") == ["spinnaker"]
    assert resolve_backend_names("harvesters") == ["harvesters"]
    assert resolve_backend_names("pycameleon") == ["pycameleon"]
    assert resolve_backend_names("fake") == ["fake"]


def test_select_fake_backend():
    enumerate_fn, factory, extension = select_backend("fake")
    assert extension == "fake"
    assert callable(enumerate_fn) and callable(factory)


def test_select_pycameleon_backend():
    # pycameleon is a core dependency, so selecting it always works and it
    # persists parameters as JSON.
    enumerate_fn, factory, extension = select_backend("pycameleon")
    assert extension == "json"
    assert callable(enumerate_fn) and callable(factory)


def test_select_harvesters_backend():
    # harvesters + genicam are core deps, so selecting it always works (whether a
    # GenTL producer is installed only affects enumeration, not availability).
    enumerate_fn, factory, extension = select_backend("harvesters")
    assert extension == "json"
    assert callable(enumerate_fn) and callable(factory)


def test_select_pycameleon_without_package_raises(monkeypatch):
    # The always-defensive import path: if the wheel were missing, selection must
    # surface a clean BackendUnavailable, never a raw ImportError.
    import octacam.cameras.pycameleon as pcmod

    monkeypatch.setattr(pcmod, "pycameleon", None)
    with pytest.raises(BackendUnavailable):
        select_backend("pycameleon")


def test_select_harvesters_without_genicam_raises(monkeypatch):
    import octacam.cameras.harvesters as hmod

    monkeypatch.setattr(hmod, "Harvester", None)
    with pytest.raises(BackendUnavailable):
        select_backend("harvesters")


def test_flir_module_imports_without_pyspin():
    # The module must import even when PySpin is absent (it is reached only via
    # the registry, which converts the missing SDK to BackendUnavailable).
    import octacam.cameras.flir as flir

    assert flir.FlirBackend.extension == "json"


def test_spinnaker_module_imports_without_sdk():
    # The ctypes binding module has no import-time dependency on the SDK (ctypes
    # is stdlib; the .so is loaded lazily), so it always imports — the registry
    # converts a missing libSpinnaker_C.so to BackendUnavailable at selection.
    import octacam.cameras.spinnaker_c as spinnaker_c

    assert spinnaker_c.SpinnakerBackend.extension == "json"


def test_select_spinnaker_without_sdk_raises(monkeypatch):
    # libSpinnaker_C.so ships with the Spinnaker SDK and is not pip-installable,
    # so it is absent in CI; selecting it must surface a clean BackendUnavailable,
    # never a raw OSError. Force the missing-SDK path so the test is deterministic
    # whether or not the SDK happens to be installed on the box running it.
    import octacam.cameras.spinnaker_c as spinnaker_c

    # A never-loaded facade + a soname that does not exist makes ctypes.CDLL fail
    # exactly as it would on a box without the SDK, regardless of this host.
    monkeypatch.setattr(spinnaker_c, "_facade", None)
    monkeypatch.setattr(spinnaker_c, "_LIB_NAME", "libSpinnaker_C_absent_for_test.so")
    with pytest.raises(BackendUnavailable):
        select_backend("spinnaker")


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


def test_teardown_backend_noop_for_non_session_backends():
    teardown_backend("basler")  # must not raise
    teardown_backend("fake")
    teardown_backend("pycameleon")
