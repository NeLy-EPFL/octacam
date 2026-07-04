"""SpinnakerBackend node/param mapping, with the ctypes binding faked.

No Spinnaker SDK or hardware: the module-level ``_Spinnaker`` binding (which is
the only thing that touches ``libSpinnaker_C.so`` via ctypes) is replaced by a
fake that operates on in-memory node maps, so the SFNC → NodeInfo mapping, the
param round-trip, the software-trigger hand-off, the frame retrieve, the clean
close, and the enumerate handle-release are all exercised in pure Python. This
mirrors ``test_harvesters_backend.py``'s fake-handle approach one seam lower: the
real ctypes ABI is exercised only by the on-rig hardware verification.
"""

import json

import numpy as np
import pytest

import octacam.cameras.spinnaker_c as sc
from octacam.cameras.base import BackendError, NodeInfo
from octacam.cameras.spinnaker_c import SpinnakerBackend


# --------------------------------------------------------------------- fakes
class FakeNode:
    """A numeric node with bounds, unit, and read/write access flags."""

    def __init__(self, value, mn=None, mx=None, inc=None, unit=None, readable=True, writable=True):
        self.value = value
        self.min = mn
        self.max = mx
        self.inc = inc
        self.unit = unit
        self.readable = readable
        self.writable = writable


class FakeEnum:
    """An enumeration node holding its current symbolic value."""

    def __init__(self, value, writable=True):
        self.value = value
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
        self.PixelFormat = FakeEnum("Mono8")
        self.AcquisitionMode = FakeEnum("Continuous")
        self.TriggerSource = FakeEnum("Line0")
        self.TriggerSelector = FakeEnum("FrameStart")
        self.TriggerMode = FakeEnum("Off")
        self.TriggerOverlap = FakeEnum("Off")
        self.StreamBufferHandlingMode = FakeEnum("OldestFirst")
        self.TriggerSoftware = FakeCommand()
        # Ships capped below the sensor's transfer ceiling; open() raises it to max.
        self.DeviceLinkThroughputLimit = FakeNode(350592000, mn=4224000, mx=384384000, inc=4224000)


class FakeImage:
    def __init__(self, array=None, timestamp=0, incomplete=False):
        self.array = array
        self.timestamp = timestamp
        self.incomplete = incomplete
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
                value=int(node.value), min=node.min, max=node.max, inc=node.inc,
                unit=None, writable=node.writable,
            )
        return NodeInfo(
            value=float(node.value), min=node.min, max=node.max, inc=None,
            unit=node.unit, writable=node.writable,
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

    def execute_command(self, nodemap, name):
        node = getattr(nodemap, name, None)
        if node is None:
            raise BackendError(f"command {name} not found")
        node.executed += 1

    # imaging
    def get_next_image(self, cam, timeout_ms):
        return cam.next_image

    def image_incomplete(self, image):
        return image.incomplete

    def image_timestamp(self, image):
        return image.timestamp

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
    backend.load_params(json.dumps({"params": {"exposure": 2222.0, "width": 640}}))
    assert nm.ExposureTime.value == 2222.0 and nm.Width.value == 640
    data = json.loads(backend.save_params())
    assert data["trigger_mode"] == "Off"
    assert data["trigger_source"] == "Line2"  # captured original at load time
    assert data["params"]["exposure"] == 2222.0 and data["params"]["width"] == 640


def test_load_params_skips_unavailable_nodes(caplog):
    nm = FakeNodeMap()
    nm.Gain = FakeNode(1.5, writable=False)  # present but not writable
    backend, _cam = _open_backend(nm)
    # A rejected node is logged and skipped, never fatal.
    backend.load_params(json.dumps({"params": {"gain": 9.0, "exposure": 3000.0}}))
    assert nm.ExposureTime.value == 3000.0 and nm.Gain.value == 1.5


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
