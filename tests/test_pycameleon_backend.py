"""pycameleon backend node/param mapping, with a mocked camera (no hardware)."""

import json
import types

import numpy as np

from octacam.cameras.pycameleon import PycameleonBackend, enumerate_pycameleon


class FakePyCam:
    """Stands in for a pycameleon PyCameleonCamera handle."""

    def __init__(self, serial="PC-1"):
        self._serial = serial
        self._int = {
            "Width": 1920,
            "Height": 1200,
            "OffsetX": 0,
            "OffsetY": 0,
            "WidthMax": 1920,
            "HeightMax": 1200,
        }
        self._float = {"ExposureTime": 5000.0, "Gain": 1.5}
        self._enum = {
            "TriggerSource": "Line1",
            "TriggerSelector": "FrameStart",
            "TriggerMode": "Off",
            "PixelFormat": "Mono8",
        }
        self.executed: list[str] = []
        self.streaming = False

    def info(self):
        return {"serial_number": self._serial}

    def open(self):
        pass

    def close(self):
        pass

    def load_context_from_camera(self):
        return "<GenApi/>"

    def load_context_from_xml(self, xml):
        self.loaded_xml = xml

    def read_integer(self, node):
        return self._int[node]

    def read_float(self, node):
        return self._float[node]

    def read_enum_as_str(self, node):
        return self._enum[node]

    def write_integer(self, node, value):
        self._int[node] = value

    def write_float(self, node, value):
        self._float[node] = value

    def write_enum_as_str(self, node, value):
        self._enum[node] = value

    def execute(self, node):
        self.executed.append(node)

    def start_streaming(self, cap):
        self.streaming = True
        return object()

    def stop_streaming(self):
        self.streaming = False

    def receive(self, rx):
        return np.zeros((4, 4), dtype=np.uint8)


def _open_backend():
    cam = FakePyCam()
    backend = PycameleonBackend(cam)
    backend.open()
    return backend, cam


def test_serial_and_open_forces_mono8():
    backend, cam = _open_backend()
    assert backend.serial_number == "PC-1"
    assert backend.is_open()
    assert cam._enum["PixelFormat"] == "Mono8"
    # The GenApi XML was fetched and cached on the first open.
    assert backend._context_xml == "<GenApi/>"


def test_read_node_types_and_width_max():
    backend, _cam = _open_backend()
    width = backend.read_node("width")
    assert width.value == 1920 and isinstance(width.value, int)
    assert width.max == 1920  # filled from SFNC WidthMax
    assert width.min is None and width.inc is None and width.unit is None
    assert width.writable is True  # open ⇒ geometry writable
    exposure = backend.read_node("exposure")
    assert exposure.value == 5000.0 and isinstance(exposure.value, float)
    assert exposure.max is None  # no bounds exposed for non-geometry nodes


def test_write_node_routes_to_int_or_float():
    backend, cam = _open_backend()
    backend.write_node("width", 800)
    assert cam._int["Width"] == 800 and isinstance(cam._int["Width"], int)
    backend.write_node("exposure", 1234.5)
    assert cam._float["ExposureTime"] == 1234.5


def test_params_round_trip_and_trigger_normalization():
    backend, _cam = _open_backend()
    backend.load_params(json.dumps({"params": {"exposure": 2222.0, "width": 640}}))
    assert backend.read_node("exposure").value == 2222.0
    assert backend.read_node("width").value == 640
    data = json.loads(backend.save_params())
    assert data["trigger_mode"] == "Off"  # normalized like the other backends
    assert data["trigger_source"] == "Line1"  # captured original, not the override
    assert data["params"]["exposure"] == 2222.0


def test_triggering_sets_software_and_defers_execute():
    backend, cam = _open_backend()
    backend.begin_software_trigger_preview()
    assert cam._enum["TriggerSelector"] == "FrameStart"
    assert cam._enum["TriggerMode"] == "On"
    assert cam._enum["TriggerSource"] == "Software"
    backend.start_grab_preview()
    assert backend.is_grabbing()
    # trigger_once only bumps the pending counter — it must NOT touch the device
    # (the execute happens in retrieve, so it can't race a concurrent receive()).
    backend.trigger_once()
    assert cam.executed == []
    assert backend._pending == 1
    backend.stop_grab()
    assert not backend.is_grabbing()
    assert not cam.streaming


def test_retrieve_returns_none_when_not_grabbing():
    backend, _cam = _open_backend()
    assert backend.retrieve(1, lambda: True) is None


def test_retrieve_executes_trigger_then_receives():
    backend, cam = _open_backend()
    backend.start_grab_preview()
    backend.trigger_once()  # arm one pending frame
    got = backend.retrieve(100, lambda: True)
    assert got is not None
    array, timestamp = got
    # retrieve fires the software trigger and receives the frame back-to-back.
    assert cam.executed == ["TriggerSoftware"]
    assert array.shape == (4, 4) and timestamp == 0  # 0 ⇒ host-time fallback
    # With no pending trigger, retrieve times out and returns None.
    assert backend.retrieve(1, lambda: True) is None
    backend.stop_grab()


def test_enumerate_sorts_then_filters(monkeypatch):
    import octacam.cameras.pycameleon as pcmod

    cams = [FakePyCam("B"), FakePyCam("A"), FakePyCam("C")]
    monkeypatch.setattr(
        pcmod, "pycameleon", types.SimpleNamespace(enumerate_cameras=lambda: cams)
    )
    out = enumerate_pycameleon()
    assert [serial for serial, _handle in out] == ["A", "B", "C"]
    filtered = enumerate_pycameleon(["C", "A"])
    assert [serial for serial, _handle in filtered] == ["C", "A"]
