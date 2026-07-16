"""Backend registry selection, the auto cascade, and unavailable handling.

Pure Python, no hardware: the vendor tiers may or may not be importable here, so
these assert the cascade *structure* and the missing-SDK → BackendUnavailable
contract rather than any particular camera being present.
"""

import logging

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
    # The GenTL "harvesters" tier was removed: every GenTL producer is a
    # user-installed, vendor-EULA'd .cti with its own quirks, and the always-present
    # pycameleon floor covers the general GenICam-USB3 camera without one.
    assert "harvesters" not in BACKENDS and "harvesters" not in CASCADE


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


def test_select_harvesters_backend_raises():
    # The harvesters tier was removed: it is no longer a known backend, so an
    # explicit selection must surface a clean BackendUnavailable.
    with pytest.raises(BackendUnavailable):
        select_backend("harvesters")


def test_select_pycameleon_without_package_raises(monkeypatch):
    # The always-defensive import path: if the wheel were missing, selection must
    # surface a clean BackendUnavailable, never a raw ImportError.
    import octacam.cameras.pycameleon as pcmod

    monkeypatch.setattr(pcmod, "pycameleon", None)
    with pytest.raises(BackendUnavailable):
        select_backend("pycameleon")


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


class _FakeBaslerDevice:
    def __init__(self, serial):
        self._serial = serial

    def GetSerialNumber(self):
        return self._serial


class _FakeTlFactory:
    """pylon transport-layer factory stand-in with no hardware.

    ``CreateDevice`` raises the real SDK exception for any serial in ``bad`` — as
    pylon does when a USB3 camera's SuperSpeed link trained down to USB 2.0 —
    and returns a sentinel handle otherwise.
    """

    def __init__(self, serials, bad):
        self._devices = [_FakeBaslerDevice(s) for s in serials]
        self._bad = set(bad)
        self.created: list[str] = []

    def EnumerateDevices(self):
        return self._devices

    def CreateDevice(self, device):
        serial = device.GetSerialNumber()
        if serial in self._bad:
            from pypylon import genicam

            raise genicam.RuntimeException(
                "Failed to open device for XML file download. Error: 'The "
                "device cannot be operated on an USB 2.0 port. The device "
                "requires an USB 3.0 compatible port.'"
            )
        self.created.append(serial)
        return ("device-handle", serial)


class _FakePylon:
    class TlFactory:
        _instance = None

        @staticmethod
        def GetInstance():
            return _FakePylon.TlFactory._instance


def _patch_basler_factory(monkeypatch, serials, bad):
    pytest.importorskip("pypylon")
    from octacam.cameras import basler

    factory = _FakeTlFactory(serials, bad)
    _FakePylon.TlFactory._instance = factory
    monkeypatch.setattr(basler, "pylon", _FakePylon)
    return factory


def test_enumerate_basler_reports_uncreatable_camera_with_none_handle(monkeypatch):
    # A camera whose SuperSpeed link fell back to USB 2.0 (CreateDevice raises)
    # is reported with a None handle — the sentinel that lets CameraSystem claim
    # the serial (so no lower cascade tier retries it) without opening it — while
    # the working cameras carry real handles. Enumeration never raises.
    from octacam.cameras.basler import enumerate_basler

    _patch_basler_factory(
        monkeypatch, ["40018619", "40018631", "40018632"], bad={"40018619"}
    )
    # Capture on the octacam logger directly, not via caplog: another test (the
    # CLI's _setup_logging) may leave propagate=False, emptying caplog's capture.
    msgs: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: msgs.append(record.getMessage())
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        out = enumerate_basler()
    finally:
        logger.removeHandler(handler)
    by_serial = dict(out)
    assert by_serial["40018619"] is None  # present but unusable
    assert by_serial["40018631"] is not None and by_serial["40018632"] is not None
    assert any("40018619" in m and "USB 2.0" in m for m in msgs)


def test_enumerate_basler_all_uncreatable_have_none_handles(monkeypatch):
    # Every camera failing the same way yields all-None handles (CameraSystem
    # then opens nothing and surfaces "no cameras were opened") rather than
    # raising a raw SDK exception out of enumeration.
    from octacam.cameras.basler import enumerate_basler

    _patch_basler_factory(monkeypatch, ["A", "B"], bad={"A", "B"})
    out = enumerate_basler()
    assert [serial for serial, _h in out] == ["A", "B"]
    assert all(handle is None for _serial, handle in out)


def test_cascade_claims_declined_camera_so_lower_tier_skips_it(monkeypatch):
    # Regression: a higher tier reporting (serial, None) — "present but unusable"
    # — must CLAIM the serial so a lower cascade tier does not pointlessly retry
    # the same broken device (for a USB3 camera on a USB 2.0 link that retry just
    # fails to open on every backend and, for pycameleon, wastes a stream-timeout
    # + destabilizes native teardown).
    from octacam.cameras import system as sysmod
    from octacam.cameras.system import CameraSystem

    def top_enum(_req):  # a vendor tier: sees SN1 but declines it (None handle)
        return [("SN1", None), ("SN2", object())]

    def floor_enum(_req):  # the pycameleon-style floor: sees everything
        return [("SN1", object()), ("SN2", object()), ("SN3", object())]

    monkeypatch.setattr(sysmod, "resolve_backend_names", lambda _b: ["top", "floor"])
    monkeypatch.setattr(
        sysmod,
        "select_backend",
        lambda name: (
            (top_enum if name == "top" else floor_enum),
            (lambda h: object()),
            "x",
        ),
    )
    sys = CameraSystem.pending()  # hardware-free shell; _enumerate opens nothing
    serials = [serial for serial, _h, _mk in sys._enumerate("auto", None)]
    assert "SN1" not in serials  # declined by 'top', NOT retried by 'floor'
    assert set(serials) == {"SN2", "SN3"}


def test_single_backend_filters_declined_camera(monkeypatch):
    # The single-backend path also drops a None-handle (declined) camera.
    from octacam.cameras import system as sysmod
    from octacam.cameras.system import CameraSystem

    def only_enum(_req):
        return [("SN1", None), ("SN2", object())]

    monkeypatch.setattr(sysmod, "resolve_backend_names", lambda _b: ["solo"])
    monkeypatch.setattr(
        sysmod, "select_backend", lambda _n: (only_enum, (lambda h: object()), "x")
    )
    sys = CameraSystem.pending()
    serials = [serial for serial, _h, _mk in sys._enumerate("solo", None)]
    assert serials == ["SN2"]


def test_describe_open_failure_usb2_is_actionable():
    from octacam.cameras.basler import _describe_open_failure

    msg = _describe_open_failure(
        "40018619", RuntimeError("cannot be operated on an USB 2.0 port")
    )
    assert "40018619" in msg
    assert "cable" in msg.lower()
    assert "5000M" in msg and "480M" in msg


def test_describe_open_failure_generic_passthrough():
    from octacam.cameras.basler import _describe_open_failure

    msg = _describe_open_failure("SN9", RuntimeError("some other boom"))
    assert "SN9" in msg
    assert "some other boom" in msg


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
