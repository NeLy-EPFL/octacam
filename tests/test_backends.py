"""Backend registry selection, the auto cascade, and unavailable handling.

Pure Python, no hardware: the vendor tiers may or may not be importable here, so
these assert the cascade *structure* and the missing-SDK → BackendUnavailable
contract rather than any particular camera being present.
"""

import logging
import os
import subprocess
import sys
import time
import types

import pytest
from helpers import wait_until

from octacam.cameras import select_backend
from octacam.cameras.registry import (
    BACKENDS,
    CASCADE,
    BackendSpec,
    BackendUnavailable,
    available_backends,
    is_auto,
    resolve_backend_names,
)


def test_param_file_extensions_cover_every_backend():
    # The transfer step lists the backends' parameter-file suffixes itself so it
    # never imports a vendor SDK. Read each backend class's `extension = "..."`
    # from the source (an SDK-free check), so a new suffix can't go missing.
    import ast
    from pathlib import Path

    import octacam.cameras
    from octacam.transform import PARAM_FILE_EXTENSIONS

    found = set()
    for source in Path(octacam.cameras.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                elif isinstance(stmt, ast.AnnAssign):
                    target = stmt.target
                else:
                    continue
                if (
                    isinstance(target, ast.Name)
                    and target.id == "extension"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    found.add(stmt.value.value)
    assert found == set(PARAM_FILE_EXTENSIONS)


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


def test_is_auto_matches_the_cascade_selectors():
    assert all(is_auto(name) for name in ("auto", " ALL ", "", None))
    assert not any(is_auto(name) for name in ("basler", "fake", "automatic"))


def test_resolve_backend_names_concrete_is_single():
    assert resolve_backend_names("basler") == ["basler"]
    assert resolve_backend_names("FLIR") == ["flir"]
    assert resolve_backend_names("spinnaker") == ["spinnaker"]
    assert resolve_backend_names("pycameleon") == ["pycameleon"]
    assert resolve_backend_names("fake") == ["fake"]


def test_select_fake_backend():
    spec = select_backend("fake")
    assert spec.factory.extension == "fake"
    assert callable(spec.enumerate) and spec.teardown is None


def test_select_pycameleon_backend():
    # pycameleon is a core dependency, so selecting it always works and it
    # persists parameters as native GenApi TSV (shared _genicam_config format).
    spec = select_backend("pycameleon")
    assert spec.factory.extension == "txt"
    assert callable(spec.enumerate) and callable(spec.read_model)


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


def test_only_the_spinnaker_tiers_hold_session_state():
    # flir and spinnaker hold the Spinnaker System until every camera is closed;
    # the other backends have nothing to release.
    for name in ("basler", "fake", "pycameleon"):
        assert select_backend(name).teardown is None
    import octacam.cameras.flir as flir
    import octacam.cameras.spinnaker_c as spinnaker_c

    assert flir.SPEC.teardown is flir.teardown
    assert spinnaker_c.SPEC.teardown is spinnaker_c.teardown


# --------------------------------------------------------------------------
# Basler backend unit tests (pypylon imports here — genicam.GenericException is
# the real SDK exception type — but no camera is present, so pylon's
# InstantCamera is replaced by a faked raw device).
# --------------------------------------------------------------------------


class _Raw:
    """A pylon InstantCamera stand-in: a GigE camera (timestamps in ticks)."""

    def GetDeviceInfo(self):
        return types.SimpleNamespace(
            GetSerialNumber=lambda: "test-basler", GetDeviceClass=lambda: "BaslerGigE"
        )


@pytest.fixture
def make_basler_backend(monkeypatch):
    pytest.importorskip("pypylon")
    from octacam.cameras import basler

    def make(raw):
        monkeypatch.setattr(basler.pylon, "InstantCamera", lambda _device: raw)
        return basler.BaslerBackend(None)

    return make


class _FakeBaslerRaw(_Raw):
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


def test_basler_start_grab_record_stops_on_ready_timeout(make_basler_backend):
    # A False ready gate must leave the camera NOT grabbing (base.start_record
    # does no stop_grab on a False return), else it wedges the device.
    raw = _FakeBaslerRaw(ready=False)
    be = make_basler_backend(raw)
    assert be.start_grab_record() is False
    assert be.is_grabbing() is False
    assert raw.stop_grabbing_calls == 1


def test_basler_start_grab_record_stops_on_ready_raise(make_basler_backend):
    # A raising ready gate must also end the grab before re-raising.
    from pypylon import genicam

    raw = _FakeBaslerRaw(ready_raises=True)
    be = make_basler_backend(raw)
    with pytest.raises(genicam.GenericException):
        be.start_grab_record()
    assert be.is_grabbing() is False
    assert raw.stop_grabbing_calls == 1


def test_basler_start_grab_record_stays_grabbing_when_ready(make_basler_backend):
    raw = _FakeBaslerRaw(ready=True)
    be = make_basler_backend(raw)
    assert be.start_grab_record() is True
    assert be.is_grabbing() is True
    assert raw.stop_grabbing_calls == 0


class _RaisingRetrieveRaw(_Raw):
    """A grabbing raw whose RetrieveResult raises a device-level SDK error."""

    def ExecuteSoftwareTrigger(self):
        pass

    def RetrieveResult(self, timeout_ms, handling):
        from pypylon import genicam

        raise genicam.GenericException("device removed", "test", 0)


def test_basler_retrieve_swallows_device_error(make_basler_backend):
    # A device error out of RetrieveResult must become one lost frame (None),
    # never propagate into the grab loop and orphan the ffmpeg writer.
    be = make_basler_backend(_RaisingRetrieveRaw())
    be.trigger.begin_grab()
    be.trigger_once()  # one pending trigger, so retrieve fires and fetches
    assert be.retrieve(50, lambda: True) is None


def test_basler_retrieve_freerun_swallows_device_error(make_basler_backend):
    be = make_basler_backend(_RaisingRetrieveRaw())
    be.trigger.begin_grab()
    assert be.retrieve_freerun(50, lambda: True) is None


class _GrabResult:
    def __init__(self, valid=True, timestamp=0):
        self._valid = valid
        self.TimeStamp = timestamp
        self.Array = None

    def IsValid(self):
        return self._valid

    def GrabSucceeded(self):
        return True

    def Release(self):
        pass


class _LateImageRaw(_Raw):
    """A raw whose RetrieveResult hands out queued results (an empty one: the
    fetch timed out) and counts the software triggers fired."""

    def __init__(self, results):
        self.results = list(results)
        self.fired = 0

    def ExecuteSoftwareTrigger(self):
        self.fired += 1

    def RetrieveResult(self, timeout_ms, handling):
        return self.results.pop(0) if self.results else _GrabResult(valid=False)


def test_basler_retrieve_credits_a_late_image_to_its_own_trigger(make_basler_backend):
    # Trigger 0's image misses the fetch after it. The next retrieve must not
    # fire trigger 1 — it would then fetch image 0 and credit it to trigger 1,
    # putting every later frame a pulse late — but only fetch.
    raw = _LateImageRaw([_GrabResult(valid=False), _GrabResult(timestamp=111)])
    be = make_basler_backend(raw)
    be.trigger.begin_grab()
    be.trigger_once()
    assert be.retrieve(50, lambda: True) is None  # fired 0; its image is late
    be.trigger_once()
    frame = be.retrieve(50, lambda: True)
    assert frame is not None and frame[1] == 111
    assert raw.fired == 1 and be.last_trigger_index == 0
    raw.results.append(_GrabResult(timestamp=222))
    frame = be.retrieve(50, lambda: True)  # now trigger 1 fires
    assert frame is not None and frame[1] == 222
    assert raw.fired == 2 and be.last_trigger_index == 1


class _RefusingRaw(_LateImageRaw):
    """A raw whose first software trigger the device refuses."""

    def __init__(self, results):
        super().__init__(results)
        self.refusals = 1

    def ExecuteSoftwareTrigger(self):
        if self.refusals:
            from pypylon import genicam

            self.refusals -= 1
            raise genicam.GenericException("trigger refused", "test", 0)
        super().ExecuteSoftwareTrigger()


def test_basler_retrieve_does_not_await_a_refused_trigger(make_basler_backend):
    raw = _RefusingRaw([_GrabResult(timestamp=111)])
    be = make_basler_backend(raw)
    be.trigger.begin_grab()
    be.trigger_once()
    assert be.retrieve(50, lambda: True) is None
    assert be.trigger.fired_index is None and raw.results  # nothing fetched
    be.trigger_once()  # fires at once: nothing awaits trigger 0's image
    frame = be.retrieve(50, lambda: True)
    assert frame is not None and frame[1] == 111
    assert raw.fired == 1 and be.last_trigger_index == 1


def test_basler_unpulsed_fetches_answer_no_trigger(make_basler_backend):
    # Free run and an external trigger fetch outside the hand-off: answering it
    # would make every frame UNMATCHED_TRIGGER, discarded as extra.
    raw = _LateImageRaw([_GrabResult(timestamp=111), _GrabResult(timestamp=222)])
    be = make_basler_backend(raw)
    be.trigger.begin_grab()
    assert be.retrieve_freerun(50, lambda: True)[1] == 111
    assert be.retrieve_external(50, lambda: True)[1] == 222
    assert be.last_trigger_index is None and raw.fired == 0


class _ClosableRaw(_FakeBaslerRaw):
    def __init__(self):
        super().__init__()
        self.destroyed = False

    def IsGrabbing(self):
        return self.start_grabbing_calls > self.stop_grabbing_calls

    def IsOpen(self):
        return True

    def Close(self):
        pass

    def DestroyDevice(self):
        self.destroyed = True


def test_basler_close_mid_grab_ends_it_and_retrieve_stays_quiet(make_basler_backend):
    raw = _ClosableRaw()
    be = make_basler_backend(raw)
    assert be.start_grab_record() is True
    be.close()
    assert be.is_grabbing() is False and raw.destroyed
    assert raw.stop_grabbing_calls == 1
    be.trigger.begin_grab()  # a stop race: the hand-off still reads grabbing
    be.trigger_once()
    assert be.retrieve(10, lambda: True) is None
    assert be.retrieve_freerun(10, lambda: True) is None


class _FakeBaslerDevice:
    def __init__(self, serial):
        self._serial = serial

    def GetSerialNumber(self):
        return self._serial

    def GetModelName(self):
        return "acA1920-150um"


class _FakeTlFactory:
    """pylon transport-layer factory stand-in with no hardware.

    ``CreateDevice`` raises the real SDK exception for any serial in ``bad`` — as
    pylon does when a USB3 camera's SuperSpeed link trained down to USB 2.0 —
    blocks for ``slow_seconds`` for any serial in ``slow`` (a camera that
    enumerated but never answers its first register read, which held one test rig
    for 271 s), and returns a sentinel handle otherwise.
    """

    def __init__(self, serials, bad, slow=(), slow_seconds=30.0):
        self._devices = [_FakeBaslerDevice(s) for s in serials]
        self._bad = set(bad)
        self._slow = set(slow)
        self._slow_seconds = slow_seconds
        self.created: list[str] = []
        self.destroyed: list[str] = []
        # GENICAM_GENTL64_PATH as pylon would see it while loading its transport
        # layers (see basler.tl_factory).
        self.gentl_path_at_load: list[str | None] = []

    def EnumerateTls(self):
        self.gentl_path_at_load.append(os.environ.get("GENICAM_GENTL64_PATH"))
        return []

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
        if serial in self._slow:
            time.sleep(self._slow_seconds)
        self.created.append(serial)
        return ("device-handle", serial)

    def DestroyDevice(self, device):
        self.destroyed.append(device[1])


class _FakePylon:
    class TlFactory:
        _instance = None

        @staticmethod
        def GetInstance():
            return _FakePylon.TlFactory._instance


def _patch_basler_factory(monkeypatch, serials, bad, slow=(), slow_seconds=30.0):
    pytest.importorskip("pypylon")
    from octacam.cameras import basler

    factory = _FakeTlFactory(serials, bad, slow=slow, slow_seconds=slow_seconds)
    _FakePylon.TlFactory._instance = factory
    monkeypatch.setattr(basler, "pylon", _FakePylon)
    monkeypatch.setattr(basler, "_tl_factory_ready", False)
    return factory


def test_tl_factory_loads_its_transport_layers_with_the_gentl_path_hidden(monkeypatch):
    # A system pylon install puts its GenTL producers on GENICAM_GENTL64_PATH;
    # pylon's GenTL transport layer loaded them, and their unload segfaulted
    # every octacam process at exit. The path is hidden while the factory loads
    # its transport layers, restored after, and touched only on first use.
    from octacam.cameras import basler

    monkeypatch.setenv("GENICAM_GENTL64_PATH", "/opt/pylon/lib/gentlproducer/gtl")
    factory = _patch_basler_factory(monkeypatch, ["40018631"], bad=set())
    assert basler.tl_factory() is factory
    assert factory.gentl_path_at_load == [None]
    assert os.environ["GENICAM_GENTL64_PATH"] == "/opt/pylon/lib/gentlproducer/gtl"
    assert basler.tl_factory() is factory
    assert factory.gentl_path_at_load == [None]  # loaded once


def test_tl_factory_restores_the_gentl_path_when_loading_fails(monkeypatch):
    from octacam.cameras import basler

    monkeypatch.setenv("GENICAM_GENTL64_PATH", "/somewhere")
    factory = _patch_basler_factory(monkeypatch, [], bad=set())

    def fail():
        raise RuntimeError("no transport layers")

    monkeypatch.setattr(factory, "EnumerateTls", fail)
    with pytest.raises(RuntimeError):
        basler.tl_factory()
    assert os.environ["GENICAM_GENTL64_PATH"] == "/somewhere"
    assert basler._tl_factory_ready is False  # the next use tries again


def test_enumerate_basler_goes_through_the_guarded_factory(monkeypatch):
    from octacam.cameras.basler import enumerate_basler

    monkeypatch.setenv("GENICAM_GENTL64_PATH", "/opt/pylon/lib/gentlproducer/gtl")
    factory = _patch_basler_factory(monkeypatch, ["40018631"], bad=set())
    assert [serial for serial, _handle in enumerate_basler(["40018631"])] == ["40018631"]
    assert factory.gentl_path_at_load == [None]


def test_doctor_reaches_pylon_through_the_guarded_factory(monkeypatch):
    # doctor's own route into pylon (cli._enumerate_backend, not enumerate_basler),
    # and its parallel scan loads the factory on the main thread, before the
    # other SDKs' workers start.
    from octacam import cli

    monkeypatch.setenv("GENICAM_GENTL64_PATH", "/opt/pylon/lib/gentlproducer/gtl")
    factory = _patch_basler_factory(monkeypatch, ["40018631"], bad=set())
    cli._CameraScan("basler")
    assert factory.gentl_path_at_load == [None]
    monkeypatch.setattr("octacam.cameras.basler._tl_factory_ready", False)
    assert cli._enumerate_backend("basler") == [("40018631", "acA1920-150um")]
    assert factory.gentl_path_at_load == [None, None]
    assert os.environ["GENICAM_GENTL64_PATH"] == "/opt/pylon/lib/gentlproducer/gtl"


def _gentl_producers_on_path() -> list[str]:
    paths = os.environ.get("GENICAM_GENTL64_PATH", "").split(os.pathsep)
    found = []
    for d in paths:
        try:
            found += [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".cti")]
        except OSError:  # missing or unreadable: collection must not fail on it
            continue
    return found


@pytest.mark.skipif(
    not _gentl_producers_on_path(), reason="no GenTL producer on GENICAM_GENTL64_PATH"
)
def test_real_pylon_loads_no_gentl_producer_and_exits_cleanly():
    # The real crash, where it can happen: a machine with a system pylon install
    # (its producers on GENICAM_GENTL64_PATH). Enumeration only, no camera opened.
    pytest.importorskip("pypylon")
    script = (
        "from octacam.cameras.basler import tl_factory\n"
        "tl_factory().EnumerateDevices()\n"
        "maps = open('/proc/self/maps').read()\n"
        "print('cti' if '.cti' in maps else 'clean', flush=True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout.strip().splitlines()[-1] == "clean"


def test_enumerate_basler_reports_uncreatable_camera_with_none_handle(
    monkeypatch, caplog
):
    # A camera whose SuperSpeed link fell back to USB 2.0 (CreateDevice raises)
    # is reported with a None handle — the sentinel that lets CameraSystem claim
    # the serial (so no lower cascade tier retries it) without opening it — while
    # the working cameras carry real handles. Enumeration never raises.
    from octacam.cameras.basler import enumerate_basler

    _patch_basler_factory(
        monkeypatch, ["40018619", "40018631", "40018632"], bad={"40018619"}
    )
    out = enumerate_basler()
    by_serial = dict(out)
    assert by_serial["40018619"] is None  # present but unusable
    assert by_serial["40018631"] is not None and by_serial["40018632"] is not None
    assert any("40018619" in m and "USB 2.0" in m for m in caplog.messages)


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
        lambda name: BackendSpec(
            top_enum if name == "top" else floor_enum, lambda h: object()
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
        sysmod, "select_backend", lambda _n: BackendSpec(only_enum, lambda h: object())
    )
    sys = CameraSystem.pending()
    serials = [serial for serial, _h, _mk in sys._enumerate("solo", None)]
    assert serials == ["SN2"]


def test_enumerate_basler_skips_a_camera_that_never_responds(monkeypatch, caplog):
    # Regression: a camera whose link trains at full SuperSpeed but whose control
    # transfers time out kept pylon retrying its first register read for 271 s
    # inside one CreateDevice, stalling the whole rig's startup. Enumeration must
    # bound that: the sick camera comes back as the "present but unusable" None
    # sentinel and the healthy ones are unaffected.
    from octacam.cameras import basler as basler_mod
    from octacam.cameras.basler import enumerate_basler

    monkeypatch.setenv("OCTACAM_BASLER_CREATE_TIMEOUT", "0.6")
    monkeypatch.setattr(basler_mod, "_CREATE_PROGRESS_INTERVAL_S", 0.05)
    factory = _patch_basler_factory(
        monkeypatch,
        ["40018619", "40018631", "40018632"],
        bad=(),
        slow={"40018632"},
        slow_seconds=5.0,
    )
    caplog.set_level(logging.DEBUG, logger="octacam")
    started = time.monotonic()
    out = enumerate_basler()
    elapsed = time.monotonic() - started

    by_serial = dict(out)
    assert by_serial["40018632"] is None  # present but unusable
    assert by_serial["40018619"] is not None and by_serial["40018631"] is not None
    # Bounded by the deadline, not by the sick camera's 5 s.
    assert elapsed < 3.0, f"enumeration took {elapsed:.1f}s; deadline was 0.6s"
    assert any("40018632" in m and "did not respond" in m for m in caplog.messages)
    # The stall is no longer silent: the operator is told who we are waiting on.
    assert any("Waiting up to" in m and "40018632" in m for m in caplog.messages)

    # The abandoned worker is still inside pylon; when it finally hands over a
    # device nothing owns it, so it must be released rather than left for the GC
    # to destroy after PylonTerminate() (that is the documented teardown crash).
    wait_until(lambda: "40018632" in factory.destroyed, timeout=20.0, interval=0.05)
    assert factory.destroyed == ["40018632"]


def test_enumerate_basler_is_quiet_when_every_camera_is_healthy(monkeypatch, caplog):
    # The progress line is time-gated: a healthy rig finishes well inside the
    # interval and must say nothing, or every normal startup cries wolf. (The
    # first version announced as soon as *any* camera was still outstanding,
    # which fired on every single startup.)
    from octacam.cameras.basler import enumerate_basler

    _patch_basler_factory(monkeypatch, ["40018619", "40018631", "40023151"], bad=())
    caplog.set_level(logging.DEBUG, logger="octacam")
    out = enumerate_basler()
    assert all(handle is not None for _serial, handle in out)
    assert not any("Waiting up to" in m for m in caplog.messages)
    assert not any("did not respond" in m for m in caplog.messages)


def test_enumerate_basler_deduplicates_a_repeated_serial(monkeypatch):
    # A serial listed twice in the rig config must create the device once. Two
    # CreateDevice calls would hand back two real handles of which only one can
    # be returned (results is keyed by serial), orphaning the other with no
    # DestroyDevice — the pylon leak that segfaults at PylonTerminate().
    from octacam.cameras.basler import enumerate_basler

    factory = _patch_basler_factory(monkeypatch, ["40018619", "40018631"], bad=())
    out = enumerate_basler(["40018619", "40018631", "40018619"])
    assert [serial for serial, _h in out] == ["40018619", "40018631"]
    assert sorted(factory.created) == ["40018619", "40018631"]


def test_enumerate_basler_create_timeout_env_override_is_validated(monkeypatch):
    # A typo must not silently disable the guard.
    from octacam.cameras.basler import _CREATE_DEVICE_TIMEOUT_S, _create_device_timeout

    monkeypatch.setenv("OCTACAM_BASLER_CREATE_TIMEOUT", "45")
    assert _create_device_timeout() == 45.0
    # float() accepts inf/nan and neither is caught by a `<= 0` test: inf would
    # restore the unbounded stall the deadline exists to prevent, and nan (which
    # compares False against everything) would spin the deadline loop forever.
    for bogus in ("abc", "0", "-3", "", "inf", "-inf", "nan", "1e400"):
        monkeypatch.setenv("OCTACAM_BASLER_CREATE_TIMEOUT", bogus)
        assert _create_device_timeout() == _CREATE_DEVICE_TIMEOUT_S
    monkeypatch.delenv("OCTACAM_BASLER_CREATE_TIMEOUT")
    assert _create_device_timeout() == _CREATE_DEVICE_TIMEOUT_S


def test_enumerate_basler_says_nothing_of_an_absent_serial(monkeypatch, caplog):
    # The cascade offers every tier the rig's whole serial list, so serials owned
    # by another backend must not be reported missing by this one: only
    # CameraSystem reports a serial no tier found.
    from octacam.cameras.basler import enumerate_basler

    _patch_basler_factory(monkeypatch, ["40018619"], bad=())
    caplog.set_level(logging.DEBUG, logger="octacam")
    out = enumerate_basler(["40018619", "17475185"])
    assert [s for s, _h in out] == ["40018619"]
    assert not any("17475185" in m for m in caplog.messages)


def test_select_serials_requested_in_order_else_all_sorted():
    from octacam.cameras.registry import select_serials

    detected = ["C", "A", "B", "A"]
    assert select_serials(detected, None) == ["A", "B", "C"]
    assert select_serials(detected, []) == ["A", "B", "C"]
    assert select_serials(detected, ["B", "Z", "A", "B"]) == ["B", "A"]


def test_cascade_does_not_enumerate_cameras_the_rig_never_asked_for(monkeypatch):
    # Regression: the cascade used to call every tier with None ("the whole bus"),
    # so a 2-camera FLIR rig paid for CreateDevice on every attached Basler — and
    # inherited the stall when one of them was sick. Each tier must be offered the
    # rig's requested serials instead.
    from octacam.cameras import system as sysmod
    from octacam.cameras.system import CameraSystem

    seen: list[tuple[str, object]] = []

    def make_enum(name, serials):
        def enumerate_fn(requested):
            seen.append((name, requested))
            pairs = [(s, object()) for s in serials]
            if requested:
                return [(s, h) for s, h in pairs if s in set(requested)]
            return pairs

        return enumerate_fn

    tiers = {"vendor": ["SN1", "SN2"], "floor": ["SN1", "SN2", "SN3", "SN4"]}
    monkeypatch.setattr(sysmod, "resolve_backend_names", lambda _b: list(tiers))
    monkeypatch.setattr(
        sysmod,
        "select_backend",
        lambda name: BackendSpec(make_enum(name, tiers[name]), lambda h: object()),
    )
    system = CameraSystem.pending()
    entries = system._enumerate("auto", ["SN1", "SN4"])

    assert [serial for serial, _h, _mk in entries] == ["SN1", "SN4"]
    # Every tier got the requested list, not None.
    assert seen == [("vendor", ["SN1", "SN4"]), ("floor", ["SN1", "SN4"])]


def test_describe_open_failure_usb2_is_actionable():
    from octacam.cameras.basler import _describe_open_failure

    msg = _describe_open_failure(
        "40018619", RuntimeError("cannot be operated on an USB 2.0 port")
    )
    assert "40018619" in msg
    assert "cable" in msg.lower()
    assert "5000M" in msg and "480M" in msg


def test_describe_open_failure_register_timeout_is_actionable():
    # The class of failure that stalled the rig: the camera enumerated and the
    # link trained, but it never answered its first register read. The old
    # message passed the raw SDK text through with no hint.
    from octacam.cameras.basler import _describe_open_failure

    msg = _describe_open_failure(
        "40018632",
        RuntimeError(
            "Failed to open device '2676:ba02:2:3:10' for XML file download. "
            "Error: 'Failed to read the first register (maximum device "
            "response time).'"
        ),
    )
    assert "40018632" in msg
    assert "cable" in msg.lower()
    assert "dmesg" in msg


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
    # An enumerate-only path (doctor/probe) never closes a CameraSystem, so the
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


# --- ROI apply order (a stale origin must not clamp the file's own size) ----- #


def test_roi_offsets_are_applied_after_sizes_whatever_the_file_order():
    """Origins must be programmed after sizes, however the file lists them.

    A size node's max is ``sensor - origin``, so an origin written first clamps
    the size that follows. ``_clear_roi_offsets`` zeroes the origins up front, but
    that only helps when the file's own origin lines come *after* its size lines —
    true for octacam's ``dump_config`` (CONFIG_NODES order) and not something a
    vendor-exported or hand-edited file guarantees, though the module advertises
    the native GenApi persistence TSV. Given ``OffsetY`` before ``Height``, the
    zeroed origin was immediately overwritten with 278 and the following
    ``Height = 2048`` was refused against a max of 1770, leaving the camera on the
    previous session's ROI for the whole recording.
    """
    from octacam.cameras._genicam_config import _roi_offsets_last

    pairs = [
        ("OffsetY", "278"),
        ("Height", "2048"),
        ("OffsetX", "64"),
        ("Width", "2048"),
        ("ExposureTime", "1000"),
    ]
    names = [name for name, _ in _roi_offsets_last(pairs)]
    assert names.index("Height") < names.index("OffsetY")
    assert names.index("Width") < names.index("OffsetX")
    # Nothing is dropped, and non-ROI nodes keep their file order.
    assert sorted(names) == sorted(n for n, _ in pairs)
    assert names.index("ExposureTime") < names.index("OffsetY")


def test_roi_reorder_is_a_no_op_for_dump_config_order():
    """octacam's own files already list sizes first; they must be untouched."""
    from octacam.cameras._genicam_config import _roi_offsets_last

    pairs = [("Width", "2048"), ("Height", "2048"), ("OffsetX", "0"), ("OffsetY", "0")]
    assert _roi_offsets_last(pairs) == pairs


def test_rejected_geometry_write_is_reported_loudly(caplog):
    """A refused Width/Height must not vanish into a debug log.

    The applier is deliberately best-effort (an unknown node on another model is
    skipped), but geometry decides what the camera actually records: a silent skip
    means the take comes out at the previous session's ROI with nothing visible to
    the operator, and the default CLI log level is info.
    """
    from octacam.cameras._genicam_config import apply_config
    from octacam.cameras.base import BackendError

    class Backend:
        serial_number = "17475185"

        def _set_number(self, name, value, is_int):
            if name == "Height":
                raise BackendError("Height = 2048 must be equal or smaller than Max")

        def _set_bool(self, name, value):
            pass

        def _set_enum(self, name, value):
            pass

    with caplog.at_level(logging.WARNING, logger="octacam"):
        apply_config(Backend(), "Width\t2048\nHeight\t2048\n")
    assert any(
        "Height" in m and "geometry" in m for m in caplog.messages
    ), caplog.messages
