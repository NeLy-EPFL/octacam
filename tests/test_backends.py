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
    # persists parameters as native GenApi TSV (shared _genicam_config format).
    enumerate_fn, factory, extension = select_backend("pycameleon")
    assert extension == "txt"
    assert callable(enumerate_fn) and callable(factory)


def test_select_harvesters_backend():
    # harvesters + genicam are core deps, so selecting it always works (whether a
    # GenTL producer is installed only affects enumeration, not availability).
    enumerate_fn, factory, extension = select_backend("harvesters")
    assert extension == "txt"
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

    assert flir.FlirBackend.extension == "txt"


def test_spinnaker_module_imports_without_sdk():
    # The ctypes binding module has no import-time dependency on the SDK (ctypes
    # is stdlib; the .so is loaded lazily), so it always imports — the registry
    # converts a missing libSpinnaker_C.so to BackendUnavailable at selection.
    import octacam.cameras.spinnaker_c as spinnaker_c

    assert spinnaker_c.SpinnakerBackend.extension == "txt"


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


# --------------------------------------------------------------------------
# Basler backend unit tests (pypylon imports here — genicam.GenericException is
# the real SDK exception type — but no camera is present, so the raw device is
# faked and the backend is built without __init__'s InstantCamera construction).
# --------------------------------------------------------------------------


def _make_basler_backend(raw):
    pytest.importorskip("pypylon")
    from octacam.cameras.basler import BaslerBackend

    be = BaslerBackend.__new__(BaslerBackend)
    be._init_trigger_handoff()
    be._serial = "test-basler"
    be._incomplete_grabs = 0
    be.raw = raw
    return be


class _FakeBaslerRaw:
    def __init__(self, *, ready=True, ready_raises=False):
        self._ready = ready
        self._ready_raises = ready_raises
        self.start_grabbing_calls = 0
        self.stop_grabbing_calls = 0

    def StartGrabbing(self, strategy):
        self.start_grabbing_calls += 1

    def StopGrabbing(self):
        self.stop_grabbing_calls += 1

    def WaitForFrameTriggerReady(self, timeout_ms, handling):
        if self._ready_raises:
            from pypylon import genicam

            raise genicam.GenericException("not ready", "test", 0)
        return self._ready


def test_basler_start_grab_record_stops_on_ready_timeout():
    # A False ready gate must leave the camera NOT grabbing (base.start_record
    # does no stop_grab on a False return), else it wedges the device.
    raw = _FakeBaslerRaw(ready=False)
    be = _make_basler_backend(raw)
    assert be.start_grab_record() is False
    assert be.is_grabbing() is False
    assert raw.stop_grabbing_calls == 1


def test_basler_start_grab_record_stops_on_ready_raise():
    # A raising ready gate must also flip _grabbing off before re-raising.
    from pypylon import genicam

    raw = _FakeBaslerRaw(ready_raises=True)
    be = _make_basler_backend(raw)
    with pytest.raises(genicam.GenericException):
        be.start_grab_record()
    assert be.is_grabbing() is False
    assert raw.stop_grabbing_calls == 1


def test_basler_start_grab_record_stays_grabbing_when_ready():
    raw = _FakeBaslerRaw(ready=True)
    be = _make_basler_backend(raw)
    assert be.start_grab_record() is True
    assert be.is_grabbing() is True
    assert raw.stop_grabbing_calls == 0


class _RaisingRetrieveRaw:
    """A grabbing raw whose RetrieveResult raises a device-level SDK error."""

    def ExecuteSoftwareTrigger(self):
        pass

    def RetrieveResult(self, timeout_ms, handling):
        from pypylon import genicam

        raise genicam.GenericException("device removed", "test", 0)


def test_basler_retrieve_swallows_device_error():
    # A device error out of RetrieveResult must become one lost frame (None),
    # never propagate into the grab loop and orphan the ffmpeg writer.
    be = _make_basler_backend(_RaisingRetrieveRaw())
    be._begin_grab()  # arm the hand-off
    be._pending = 1  # one pending trigger so _wait_pending returns True
    assert be.retrieve(50, lambda: True) is None


def test_basler_retrieve_freerun_swallows_device_error():
    be = _make_basler_backend(_RaisingRetrieveRaw())
    be._begin_grab()
    assert be.retrieve_freerun(50, lambda: True) is None


# --------------------------------------------------------------------------
# FLIR backend unit tests (PySpin is absent here, so _spin() is faked).
# --------------------------------------------------------------------------


class _FakeSystem:
    def __init__(self, cam_list):
        self._cam_list = cam_list
        self.released = 0

    def GetCameras(self):
        return self._cam_list

    def ReleaseInstance(self):
        self.released += 1


class _FakeCamList:
    def __init__(self, size=0):
        self._size = size
        self.cleared = 0

    def GetSize(self):
        return self._size

    def Clear(self):
        self.cleared += 1


def test_flir_enumerate_releases_previous_system(monkeypatch):
    # Re-enumeration (octacam doctor enumerates twice) must release the prior
    # System first instead of orphaning it. Fix mirrors spinnaker_c's guard.
    import octacam.cameras.flir as flir

    prev_system = _FakeSystem(_FakeCamList())
    prev_list = _FakeCamList()
    monkeypatch.setattr(flir, "_system", prev_system)
    monkeypatch.setattr(flir, "_cam_list", prev_list)

    new_system = _FakeSystem(_FakeCamList(size=0))

    class _FakeSpin:
        class System:
            @staticmethod
            def GetInstance():
                return new_system

    monkeypatch.setattr(flir, "_spin", lambda: _FakeSpin)

    assert flir.enumerate_flir(None) == []
    # The stale session was torn down before GetInstance ran again.
    assert prev_system.released == 1
    assert prev_list.cleared == 1


def test_flir_registers_atexit_teardown(monkeypatch):
    # An enumerate-only path (doctor/probe) never runs teardown_backend, so the
    # module must net the System release at interpreter shutdown. Reload the
    # module with a recording atexit.register to prove it registers teardown().
    import atexit
    import importlib

    import octacam.cameras.flir as flir

    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn))
    try:
        reloaded = importlib.reload(flir)
        assert reloaded.teardown in registered
    finally:
        monkeypatch.undo()
        importlib.reload(flir)  # restore the real module + its real atexit net


class _FakeImage:
    def __init__(self, arr):
        self._arr = arr
        self.released = 0

    def IsIncomplete(self):
        return False

    def GetTimeStamp(self):
        return 12345

    def GetNDArray(self):
        return self._arr

    def Release(self):
        self.released += 1


class _FakeCam:
    def __init__(self, image):
        self._image = image

    def GetNextImage(self, timeout_ms):
        return self._image


def _make_flir_backend(monkeypatch):
    import octacam.cameras.flir as flir

    class _FakeSpin:
        class SpinnakerException(Exception):
            pass

    monkeypatch.setattr(flir, "_spin", lambda: _FakeSpin)
    be = flir.FlirBackend.__new__(flir.FlirBackend)
    be._serial = "test-flir"
    return be


def test_flir_fetch_image_rejects_mono16(monkeypatch):
    # A 2-D uint16 (Mono8-set failed) frame passes the old ndim!=2 guard and is
    # recorded as garbage; the tightened itemsize guard must drop it.
    import numpy as np


    be = _make_flir_backend(monkeypatch)
    img = _FakeImage(np.zeros((4, 4), dtype=np.uint16))
    assert be._fetch_image(_FakeCam(img), 50, lambda: True) is None
    assert img.released == 1


def test_flir_fetch_image_accepts_mono8(monkeypatch):
    import numpy as np

    be = _make_flir_backend(monkeypatch)
    arr = np.zeros((4, 4), dtype=np.uint8)
    img = _FakeImage(arr)
    frame = be._fetch_image(_FakeCam(img), 50, lambda: True)
    assert frame is not None
    array, ts = frame
    assert array.dtype == np.uint8 and ts == 12345
    assert img.released == 1
