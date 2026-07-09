"""SpinnakerBackend node/param mapping, with the ctypes binding faked.

No Spinnaker SDK or hardware: the module-level ``_Spinnaker`` binding (which is
the only thing that touches ``libSpinnaker_C.so`` via ctypes) is replaced by a
fake that operates on in-memory node maps, so the SFNC → NodeInfo mapping, the
param round-trip, the software-trigger hand-off, the frame retrieve, the clean
close, and the enumerate handle-release are all exercised in pure Python. This
mirrors ``test_harvesters_backend.py``'s fake-handle approach one seam lower: the
real ctypes ABI is exercised only by the on-rig hardware verification.
"""

import numpy as np
import pytest

import octacam.cameras.spinnaker_c as sc
from octacam.cameras._genicam_config import parse_config
from octacam.cameras.base import BackendError, FeatureInfo, NodeInfo
from octacam.cameras.spinnaker_c import SpinnakerBackend


# --------------------------------------------------------------------- fakes
class FakeNode:
    """A numeric node with bounds, unit, and read/write access flags."""

    def __init__(
        self, value, mn=None, mx=None, inc=None, unit=None, readable=True, writable=True
    ):
        self.value = value
        self.min = mn
        self.max = mx
        self.inc = inc
        self.unit = unit
        self.readable = readable
        self.writable = writable


class FakeEnum:
    """An enumeration node holding its current symbolic value."""

    def __init__(self, value, writable=True, entries=None):
        self.value = value
        self.readable = True
        self.writable = writable
        # Selectable symbolics for the Camera-tab dropdown; defaults to the
        # current value so a walk always yields at least one entry.
        self.entries = entries if entries is not None else [value]


class FakeBool:
    """A boolean node (the native-TSV config persists these via _set/_get_bool)."""

    def __init__(self, value, writable=True):
        self.value = bool(value)
        self.readable = True
        self.writable = writable


class FakeCommand:
    def __init__(self):
        self.executed = 0


class FakeNodeMap:
    """A superset SFNC node map used for both the feature and stream node maps."""

    def __init__(self):
        self.Width = FakeNode(1920, mn=16, mx=1920, inc=16)
        self.Height = FakeNode(1200, mn=16, mx=1200, inc=8)
        self.OffsetX = FakeNode(0, mn=0, mx=1904, inc=4)
        self.OffsetY = FakeNode(0, mn=0, mx=1184, inc=2)
        self.ExposureTime = FakeNode(5000.0, mn=20.0, mx=1e6, unit="us")
        self.Gain = FakeNode(1.5, mn=0.0, mx=24.0, unit="dB")
        self.GammaEnabled = FakeBool(False)  # a config-persisted boolean node
        self.PixelFormat = FakeEnum("Mono8")
        self.AcquisitionMode = FakeEnum("Continuous")
        self.TriggerSource = FakeEnum("Line0")
        self.TriggerSelector = FakeEnum("FrameStart")
        self.TriggerMode = FakeEnum("Off")
        self.TriggerOverlap = FakeEnum("Off")
        self.StreamBufferHandlingMode = FakeEnum("OldestFirst")
        self.TriggerSoftware = FakeCommand()
        # Ships capped below the sensor's transfer ceiling; open() raises it to max.
        self.DeviceLinkThroughputLimit = FakeNode(
            350592000, mn=4224000, mx=384384000, inc=4224000
        )


class FakeImage:
    def __init__(self, array=None, timestamp=0, incomplete=False, bits=8):
        self.array = array
        self.timestamp = timestamp
        self.incomplete = incomplete
        self.bits = bits  # bits per pixel; 8 == Mono8 (the only format we accept)
        self.released = False


class FakeCam:
    def __init__(self, serial, nodemap=None):
        self.serial = serial
        self.nodemap = nodemap or FakeNodeMap()
        self.stream_nodemap = FakeNodeMap()
        self.initialized = False
        self.streaming = False
        self.released = False
        self.next_image = None


class FakeSpin:
    """Stand-in for the ctypes ``_Spinnaker`` binding.

    Every method takes the same opaque handles the real one does (here plain
    Python objects) and returns the same Python values, raising ``BackendError``
    where the real binding would on a non-success ``spinError``.
    """

    def __init__(self):
        self.cameras: list[FakeCam] = []

    # system / camera list
    def get_system(self):
        return "SYSTEM"

    def system_release_instance(self, hsystem):
        self.system_released = True

    def create_camera_list(self):
        return "CAMLIST"

    def system_get_cameras(self, hsystem, hcamlist):
        pass

    def camera_list_size(self, hcamlist):
        return len(self.cameras)

    def camera_list_get(self, hcamlist, index):
        return self.cameras[index]

    def camera_list_clear(self, hcamlist):
        pass

    def camera_list_destroy(self, hcamlist):
        pass

    def read_serial(self, cam):
        return cam.serial

    # camera lifecycle
    def camera_init(self, cam):
        cam.initialized = True

    def camera_deinit(self, cam):
        cam.initialized = False

    def camera_release(self, cam):
        cam.released = True

    def camera_is_initialized(self, cam):
        return cam.initialized

    def camera_is_streaming(self, cam):
        return cam.streaming

    def camera_get_nodemap(self, cam):
        return cam.nodemap

    def camera_get_tl_stream_nodemap(self, cam):
        return cam.stream_nodemap

    def begin_acquisition(self, cam):
        cam.streaming = True

    def end_acquisition(self, cam):
        cam.streaming = False

    # node I/O
    def read_number(self, nodemap, name, is_int):
        node = getattr(nodemap, name, None)
        if node is None or not node.readable:
            raise BackendError(f"node {name} is not readable")
        if is_int:
            return NodeInfo(
                value=int(node.value),
                min=node.min,
                max=node.max,
                inc=node.inc,
                unit=None,
                writable=node.writable,
            )
        return NodeInfo(
            value=float(node.value),
            min=node.min,
            max=node.max,
            inc=None,
            unit=node.unit,
            writable=node.writable,
        )

    def write_number(self, nodemap, name, value, is_int):
        node = getattr(nodemap, name, None)
        if node is None or not node.writable:
            raise BackendError(f"node {name} is not writable")
        node.value = int(value) if is_int else float(value)

    def set_enum(self, nodemap, name, value):
        node = getattr(nodemap, name, None)
        if node is None or not node.writable:
            raise BackendError(f"enumeration {name} is not writable")
        node.value = value

    def get_enum(self, nodemap, name):
        node = getattr(nodemap, name, None)
        return node.value if node is not None else None

    def set_bool(self, nodemap, name, value):
        node = getattr(nodemap, name, None)
        if node is None or not node.writable:
            raise BackendError(f"boolean {name} is not writable")
        node.value = bool(value)

    def get_bool(self, nodemap, name):
        node = getattr(nodemap, name, None)
        return bool(node.value) if node is not None and node.readable else None

    def read_string(self, nodemap, name):
        # e.g. DeviceModelName in save_params(); absent on the fake node map -> None.
        node = getattr(nodemap, name, None)
        return getattr(node, "value", None) if node is not None else None

    def execute_command(self, nodemap, name):
        node = getattr(nodemap, name, None)
        if node is None:
            raise BackendError(f"command {name} not found")
        node.executed += 1

    # full node-map walk (Camera tab). The real facade traverses the GenApi
    # category tree over ctypes; here we synthesize a FeatureInfo per fake node so
    # the backend delegation + widget-kind mapping are exercised in pure Python.
    def _feature_from(self, name, node):
        if isinstance(node, FakeCommand):
            return FeatureInfo(name, name, "command", category="Other")
        if isinstance(node, FakeBool):
            return FeatureInfo(
                name, name, "bool", category="Other",
                value=node.value, readable=node.readable, writable=node.writable,
            )
        if isinstance(node, FakeEnum):
            entries = [{"value": e, "display": e, "available": True} for e in node.entries]
            return FeatureInfo(
                name, name, "enum", category="Other", value=node.value,
                entries=entries, readable=node.readable, writable=node.writable,
            )
        if isinstance(node, FakeNode):
            kind = "int" if isinstance(node.value, int) else "float"
            # The real facade gives int nodes an increment but no unit, and float
            # nodes a unit but no increment (the C API has no spinFloatGetInc /
            # spinIntegerGetUnit) — mirror that so the fake is not more permissive.
            return FeatureInfo(
                name, name, kind, category="Other", value=node.value,
                min=node.min, max=node.max,
                inc=(node.inc if kind == "int" else None),
                unit=(None if kind == "int" else node.unit),
                readable=node.readable, writable=node.writable,
            )
        return None

    def list_features(self, nodemap):
        out = []
        for name, node in vars(nodemap).items():
            feature = self._feature_from(name, node)
            if feature is not None:
                out.append(feature)
        return out

    def read_feature(self, nodemap, name):
        node = getattr(nodemap, name, None)
        if node is None:
            raise BackendError(f"node {name} is not an editable feature")
        feature = self._feature_from(name, node)
        if feature is None:
            raise BackendError(f"node {name} is not an editable feature")
        return feature

    def write_feature(self, nodemap, name, value):
        node = getattr(nodemap, name, None)
        if node is None:
            raise BackendError(f"no such node: {name}")
        if not getattr(node, "writable", False):
            raise BackendError(f"node {name} is not writable")
        if isinstance(node, FakeBool):
            node.value = str(value).strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
        elif isinstance(node, FakeEnum):
            node.value = str(value)
        elif isinstance(node, FakeNode):
            node.value = int(round(float(value))) if isinstance(node.value, int) else float(value)
        else:
            raise BackendError(f"node {name} is not writable")

    # imaging
    def get_next_image(self, cam, timeout_ms):
        return cam.next_image

    def image_incomplete(self, image):
        return image.incomplete

    def image_timestamp(self, image):
        return image.timestamp

    def image_bits_per_pixel(self, image):
        return image.bits

    def image_array(self, image):
        return np.array(image.array, copy=True)

    def image_release(self, image):
        image.released = True


@pytest.fixture(autouse=True)
def fake_facade(monkeypatch):
    """Swap the real ctypes binding for the fake and reset session globals."""
    monkeypatch.setattr(sc, "_facade", FakeSpin())
    monkeypatch.setattr(sc, "_system", None)
    monkeypatch.setattr(sc, "_cam_list", None)
    monkeypatch.setattr(sc, "_outstanding", {})
    yield sc._facade


def _open_backend(nodemap=None, serial="17475185"):
    cam = FakeCam(serial, nodemap or FakeNodeMap())
    backend = SpinnakerBackend(cam)
    backend.open()
    return backend, cam


def _grabbing_backend(nodemap=None):
    backend, cam = _open_backend(nodemap)
    backend.begin_software_trigger_preview()
    backend.start_grab_preview()
    return backend, cam


# --------------------------------------------------------------------- tests
def test_open_fetches_nodemaps_and_forces_mono8():
    nm = FakeNodeMap()
    nm.PixelFormat = FakeEnum("BayerRG8")
    backend, cam = _open_backend(nm)
    assert backend.serial_number == "17475185" and backend.is_open()
    assert cam.initialized and nm.PixelFormat.value == "Mono8"
    assert backend.width() == 1920 and backend.height() == 1200


def test_open_maximizes_link_throughput():
    # FLIR ships DeviceLinkThroughputLimit capped below the sensor's transfer
    # ceiling; open() raises it to the node max so a short-exposure grab is not
    # throttled below the camera's rated frame rate. Best-effort: a model without
    # the node (see below) just keeps its default.
    nm = FakeNodeMap()
    assert nm.DeviceLinkThroughputLimit.value == 350592000  # as shipped
    _open_backend(nm)
    assert nm.DeviceLinkThroughputLimit.value == 384384000  # raised to max


def test_open_without_throughput_node_is_fine():
    # A model lacking DeviceLinkThroughputLimit must still open cleanly (the raise
    # is best-effort — read_number raises, the backend swallows it).
    nm = FakeNodeMap()
    del nm.DeviceLinkThroughputLimit
    backend, _cam = _open_backend(nm)
    assert backend.is_open()


def test_read_node_maps_bounds_and_unit_by_kind():
    backend, _cam = _open_backend()
    width = backend.read_node("width")
    # Integer nodes: bounds + increment, no unit (the C API has no int GetUnit).
    assert width.value == 1920 and width.min == 16 and width.max == 1920
    assert width.inc == 16 and width.unit is None and width.writable is True
    exposure = backend.read_node("exposure")
    # Float nodes: bounds + unit, no increment (the C API has no float GetInc).
    assert exposure.value == 5000.0 and exposure.unit == "us" and exposure.inc is None


def test_writability_reflects_access_mode():
    nm = FakeNodeMap()
    nm.Gain = FakeNode(1.5, writable=False)
    backend, _cam = _open_backend(nm)
    assert backend.read_node("gain").writable is False


def test_read_node_raises_when_camera_not_open():
    cam = FakeCam("17475185")
    backend = SpinnakerBackend(cam)  # not opened: no nodemap
    with pytest.raises(BackendError):
        backend.read_node("width")


def test_write_node_routes_to_int_or_float():
    nm = FakeNodeMap()
    backend, _cam = _open_backend(nm)
    backend.write_node("width", 800)
    assert nm.Width.value == 800 and isinstance(nm.Width.value, int)
    backend.write_node("exposure", 1234.5)
    assert nm.ExposureTime.value == 1234.5 and isinstance(nm.ExposureTime.value, float)


def test_params_round_trip():
    nm = FakeNodeMap()
    nm.TriggerSource = FakeEnum("Line2")
    backend, _cam = _open_backend(nm)
    # Native GenApi persistence TSV: tab-separated <FeatureName>\t<Value> lines,
    # applied best-effort in file order. Covers a float, an int, and a bool node.
    backend.load_params(
        "# GenApi persistence file\n"
        "ExposureTime\t2222.0\n"
        "Width\t640\n"
        "GammaEnabled\ttrue\n"
    )
    assert nm.ExposureTime.value == 2222.0 and nm.Width.value == 640
    assert nm.GammaEnabled.value is True
    # save_params round-trips to the same TSV; parse it back to name -> value.
    values = dict(parse_config(backend.save_params()))
    assert values["ExposureTime"] == "2222"  # _fmt_float drops the trailing .0
    assert values["Width"] == "640"
    assert values["GammaEnabled"] == "true"
    assert values["TriggerSource"] == "Line2"  # enum read straight back


def test_save_params_undoes_preview_software_trigger_source():
    # A config saved while the live software-trigger preview forced
    # TriggerSource=Software must NOT bake that in: save_params rewrites it back to
    # the hardware line load_params captured, so a later external-trigger recording
    # still fires. Regression: the omniview FLIR .txt shipped with Software and the
    # cameras silently never triggered.
    nm = FakeNodeMap()  # TriggerSource ships as the hardware line Line0
    backend, _cam = _open_backend(nm)
    backend.load_params("TriggerSource\tLine0\n")  # captures _original = Line0
    backend.begin_software_trigger_preview()  # forces the live source to Software
    assert nm.TriggerSource.value == "Software"
    values = dict(parse_config(backend.save_params()))
    assert values["TriggerSource"] == "Line0"


def test_save_params_keeps_software_when_that_is_the_configured_source():
    # A genuinely software-triggered rig (no hardware source to restore) is left
    # untouched, so the normalization never fights an intentional Software config.
    nm = FakeNodeMap()
    nm.TriggerSource = FakeEnum("Software")
    backend, _cam = _open_backend(nm)
    backend.load_params("TriggerSource\tSoftware\n")  # captures _original = Software
    values = dict(parse_config(backend.save_params()))
    assert values["TriggerSource"] == "Software"


def test_load_params_skips_unavailable_nodes(caplog):
    nm = FakeNodeMap()
    nm.Gain = FakeNode(1.5, writable=False)  # present but not writable
    backend, _cam = _open_backend(nm)
    # A rejected node is logged at debug and skipped, never fatal; the writable
    # lines still apply (mirrors the FLIR C config tool's per-node guard).
    backend.load_params("Gain\t9.0\nExposureTime\t3000.0\n")
    assert nm.ExposureTime.value == 3000.0 and nm.Gain.value == 1.5


# ------------------------------------------------ full node-map walk (Camera tab)
def test_list_features_walks_the_full_node_map():
    # The Camera tab now browses the whole GenApi node map (like Basler), not just
    # the six curated PARAM_NODES. Every node kind is classified correctly.
    backend, _cam = _open_backend()
    features = {f.name: f for f in backend.list_features()}
    # Far more than the six curated params.
    assert len(features) > 6
    width = features["Width"]
    assert width.type == "int" and width.value == 1920
    assert width.min == 16 and width.max == 1920 and width.inc == 16
    exposure = features["ExposureTime"]
    assert exposure.type == "float" and exposure.value == 5000.0 and exposure.unit == "us"
    assert features["Gain"].type == "float"
    gamma = features["GammaEnabled"]
    assert gamma.type == "bool" and gamma.value is False
    pixel_format = features["PixelFormat"]
    assert pixel_format.type == "enum" and pixel_format.value == "Mono8"
    assert pixel_format.entries and pixel_format.entries[0]["value"] == "Mono8"
    trigger_sw = features["TriggerSoftware"]
    assert trigger_sw.type == "command" and trigger_sw.value is None


def test_list_features_empty_when_not_open():
    cam = FakeCam("17475185")
    backend = SpinnakerBackend(cam)  # not opened: no nodemap
    assert backend.list_features() == []


def test_read_feature_returns_one_node():
    backend, _cam = _open_backend()
    feature = backend.read_feature("ExposureTime")
    assert feature.name == "ExposureTime" and feature.type == "float"
    assert feature.value == 5000.0


def test_read_feature_unknown_node_raises():
    backend, _cam = _open_backend()
    with pytest.raises(BackendError):
        backend.read_feature("NoSuchNode")


def test_read_feature_raises_when_not_open():
    backend = SpinnakerBackend(FakeCam("17475185"))
    with pytest.raises(BackendError):
        backend.read_feature("Width")


def test_write_feature_dispatches_by_node_type():
    nm = FakeNodeMap()
    backend, _cam = _open_backend(nm)
    backend.write_feature("Width", 640)  # int
    assert nm.Width.value == 640 and isinstance(nm.Width.value, int)
    backend.write_feature("Gain", 3.5)  # float
    assert nm.Gain.value == 3.5 and isinstance(nm.Gain.value, float)
    backend.write_feature("GammaEnabled", True)  # bool
    assert nm.GammaEnabled.value is True
    backend.write_feature("AcquisitionMode", "SingleFrame")  # enum
    assert nm.AcquisitionMode.value == "SingleFrame"


def test_write_feature_not_writable_raises():
    nm = FakeNodeMap()
    nm.Gain = FakeNode(1.5, writable=False)
    backend, _cam = _open_backend(nm)
    with pytest.raises(BackendError):
        backend.write_feature("Gain", 2.0)


def test_write_feature_raises_when_not_open():
    backend = SpinnakerBackend(FakeCam("17475185"))
    with pytest.raises(BackendError):
        backend.write_feature("Width", 640)


def test_execute_command_fires_the_node():
    nm = FakeNodeMap()
    backend, _cam = _open_backend(nm)
    backend.execute_command("TriggerSoftware")
    assert nm.TriggerSoftware.executed == 1


def test_execute_command_raises_when_not_open():
    backend = SpinnakerBackend(FakeCam("17475185"))
    with pytest.raises(BackendError):
        backend.execute_command("TriggerSoftware")


def test_snap_int_rounds_to_the_increment_grid():
    # The real write_feature snaps an off-grid integer to the node's inc grid
    # (offset from min) before the SDK write, which would otherwise reject it.
    # This is the one non-trivial bit of arithmetic in the ctypes walk, so it is
    # covered directly (the ctypes .so itself is verified on hardware).
    assert sc._snap_int(643, node_min=0, node_inc=16) == 640  # nearest multiple
    assert sc._snap_int(650, node_min=0, node_inc=16) == 656  # rounds up
    assert sc._snap_int(101, node_min=5, node_inc=16) == 101  # grid offset by min
    assert sc._snap_int(100, node_min=5, node_inc=16) == 101  # -> 5 + 6*16
    assert sc._snap_int(123.9, node_min=None, node_inc=None) == 124  # no inc: round
    assert sc._snap_int(200, node_min=0, node_inc=None) == 200


def test_trigger_once_is_a_pure_bump_not_a_device_call():
    # The device TriggerSoftware execute happens in retrieve() on the grab thread,
    # NOT in trigger_once() on the shared timer thread — so one camera's slow
    # trigger can never block another's. trigger_once only bumps the counter.
    nm = FakeNodeMap()
    backend, _cam = _grabbing_backend(nm)
    assert nm.TriggerSource.value == "Software" and nm.TriggerMode.value == "On"
    # TriggerOverlap=ReadOut lets triggers pipeline during readout — without it the
    # FLIR ignores every other software trigger (~halved fps; measured 5->64 fps).
    assert nm.TriggerOverlap.value == "ReadOut"
    assert backend.is_grabbing()
    backend.trigger_once()
    assert nm.TriggerSoftware.executed == 0  # NOT fired yet — retrieve fires it
    assert backend._pending == 1
    backend.stop_grab()
    assert not backend.is_grabbing()


def test_retrieve_fires_trigger_and_returns_frame():
    nm = FakeNodeMap()
    backend, cam = _grabbing_backend(nm)
    image = FakeImage(array=np.arange(6, dtype=np.uint8).reshape(2, 3), timestamp=123)
    cam.next_image = image
    backend.trigger_once()  # arm one pending frame
    frame = backend.retrieve(100, lambda: True)
    assert frame is not None
    array, timestamp = frame
    assert timestamp == 123 and array.shape == (2, 3)
    assert nm.TriggerSoftware.executed == 1  # retrieve fired the device trigger
    assert backend._pending == 0  # consumed
    assert image.released  # every image is released


def test_retrieve_skips_copy_when_display_slot_full():
    backend, cam = _grabbing_backend()
    image = FakeImage(array=np.zeros((2, 3), dtype=np.uint8), timestamp=7)
    cam.next_image = image
    backend.trigger_once()
    array, timestamp = backend.retrieve(100, lambda: False)
    assert array is None and timestamp == 7  # timestamp still recorded
    assert image.released


def test_retrieve_none_without_a_pending_trigger():
    nm = FakeNodeMap()
    backend, cam = _grabbing_backend(nm)
    cam.next_image = FakeImage(array=np.zeros((2, 3), dtype=np.uint8))
    # No trigger armed: retrieve must not fire the device or fetch a frame.
    assert backend.retrieve(1, lambda: True) is None
    assert nm.TriggerSoftware.executed == 0


def test_retrieve_none_on_grab_timeout():
    nm = FakeNodeMap()
    backend, cam = _grabbing_backend(nm)
    cam.next_image = None  # get_next_image returns None (timeout)
    backend.trigger_once()
    assert backend.retrieve(1, lambda: True) is None
    assert nm.TriggerSoftware.executed == 1  # trigger fired; frame just didn't arrive


def test_retrieve_skips_incomplete_image_but_releases_it():
    backend, cam = _grabbing_backend()
    image = FakeImage(array=np.zeros((2, 3), dtype=np.uint8), incomplete=True)
    cam.next_image = image
    backend.trigger_once()
    assert backend.retrieve(100, lambda: True) is None
    assert image.released  # incomplete frames are still released


def test_retrieve_skips_non_mono8_frame_but_releases_it():
    # We force Mono8 at open, but if that best-effort set failed the camera could
    # still deliver e.g. Mono16 — a 2-D frame with a wider stride that image_array
    # would misread one byte per pixel. The bits-per-pixel guard drops it (PySpin's
    # ndim!=2 check would let it through, since a Mono16 frame is still 2-D).
    backend, cam = _grabbing_backend()
    image = FakeImage(array=np.zeros((2, 3), dtype=np.uint8), timestamp=9, bits=16)
    cam.next_image = image
    backend.trigger_once()
    assert backend.retrieve(100, lambda: True) is None  # non-Mono8: dropped
    assert image.released  # but still released — never leak a buffer


def test_retrieve_accepts_non_mono8_frame_when_array_not_wanted():
    # The format guard lives on the array-materialization path (matching PySpin):
    # when the display slot is full we never touch the pixels, so the frame's
    # timestamp is still recorded and only the copy is skipped.
    backend, cam = _grabbing_backend()
    image = FakeImage(array=np.zeros((2, 3), dtype=np.uint8), timestamp=11, bits=16)
    cam.next_image = image
    backend.trigger_once()
    array, timestamp = backend.retrieve(100, lambda: False)
    assert array is None and timestamp == 11
    assert image.released


def test_close_deinits_releases_and_is_idempotent():
    backend, cam = _open_backend()
    backend.close()
    assert not backend.is_open()
    assert cam.initialized is False and cam.released is True
    backend.close()  # second close is a no-op


def test_enumerate_selects_requested_and_releases_the_rest(fake_facade):
    cams = [FakeCam("A"), FakeCam("B"), FakeCam("C")]
    fake_facade.cameras = cams
    out = sc.enumerate_spinnaker(["B"])
    assert [serial for serial, _h in out] == ["B"]
    assert out[0][1] is cams[1]
    # Handles not handed to a backend are released 1:1; the selected one is not.
    assert cams[0].released and cams[2].released and not cams[1].released


def test_teardown_releases_leaked_handle(fake_facade):
    # A handle enumerate hands out but that is never close()d (an enumerate-only
    # probe, or a killed/hung record) must be released by teardown() — otherwise
    # the System is released with a dangling device reference and Spinnaker's
    # libusb transport aborts the process (usbi_mutex_destroy assertion, 134).
    cams = [FakeCam("A")]
    fake_facade.cameras = cams
    out = sc.enumerate_spinnaker(["A"])
    assert out[0][1] is cams[0] and not cams[0].released  # handed out, still open
    sc.teardown()
    assert cams[0].released  # teardown released the leaked handle
    assert not sc._outstanding  # and forgot it


def test_teardown_does_not_double_release_closed_handle(fake_facade):
    # close() drops its handle from the outstanding set, so teardown() must not
    # release it a second time (a double spinCameraRelease is itself an error).
    cams = [FakeCam("A")]
    fake_facade.cameras = cams
    out = sc.enumerate_spinnaker(["A"])
    backend = SpinnakerBackend(out[0][1])
    backend.close()
    assert cams[0].released
    cams[0].released = False  # sentinel: detect any further release in teardown
    sc.teardown()
    assert cams[0].released is False  # teardown left the already-closed handle alone


def test_reenumerate_releases_prior_session(fake_facade):
    # octacam doctor enumerates spinnaker twice (the backend sweep AND the cascade
    # assignment). The second enumeration must release the first session's System
    # and handles instead of orphaning them — a stale System released only at
    # process exit aborts via a libusb assertion (exit 134).
    first = [FakeCam("A")]
    fake_facade.cameras = first
    out1 = sc.enumerate_spinnaker(["A"])
    assert out1[0][1] is first[0] and not first[0].released  # handed out, tracked
    second = [FakeCam("A")]
    fake_facade.cameras = second
    sc.enumerate_spinnaker(["A"])
    assert first[0].released  # prior session's handle released by the re-enumerate
    assert fake_facade.system_released  # and its System
    assert not second[0].released  # the fresh handle is now the outstanding one


def test_enumerate_sorts_when_unrequested():
    fake = sc._facade
    fake.cameras = [FakeCam("17475187"), FakeCam("17475185")]
    out = sc.enumerate_spinnaker()
    assert [serial for serial, _h in out] == ["17475185", "17475187"]


def test_enumerate_empty_returns_nothing():
    sc._facade.cameras = []
    assert sc.enumerate_spinnaker() == []
    # An empty enumeration tears the session down so a later run starts clean.
    assert sc._system is None and sc._cam_list is None
