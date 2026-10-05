"""FlirBackend over the PySpin binding, with PySpin faked.

No Spinnaker SDK or hardware: the module-level ``PySpin`` (the only thing the
PySpin binding touches the SDK through) is replaced by a fake whose nodes model
the one GenICam behaviour that matters here — a ROI *size* node's max is
``sensor - origin``, so a stale origin makes a full-sensor Width/Height write out
of range. That is the constraint the rig hit (``Value = 2048 must be equal or
smaller than Max = 1770`` on Height), and it is reproduced here in pure Python.
"""

import logging
from types import SimpleNamespace

import numpy as np
import pytest

import octacam.cameras.flir as flir
from octacam.cameras.base import BackendError
from octacam.cameras.flir import MIN_STREAM_BUFFERS, RECORD_STREAM_BUFFERS
from octacam.cameras.genicam import parse_config

SENSOR = 2048
BEGINNER, EXPERT, GURU, INVISIBLE = range(4)


# --------------------------------------------------------------------- fakes
class FakeSpinnakerException(Exception):
    """Stands in for PySpin.SpinnakerException (a GenICam OutOfRangeException)."""


class Node:
    """INode metadata; the interface types are the kind names themselves."""

    iface = ""

    def __init__(self, writable=True, visibility=BEGINNER):
        self.name = ""  # set by the node map
        self.writable = writable
        self.readable = True
        self.visibility = visibility

    def GetPrincipalInterfaceType(self):
        return self.iface

    def GetName(self):
        return self.name

    def GetDisplayName(self):
        return f"{self.name} (display)"

    def GetToolTip(self):
        return ""

    def GetDescription(self):
        return f"About {self.name}"

    def GetVisibility(self):
        return self.visibility

    def IsFeature(self):
        return True


class IntNode(Node):
    """An integer node whose ceiling is computed live, as GenApi's is."""

    iface = "int"

    def __init__(self, value, max_fn, writable=True, **kw):
        super().__init__(writable, **kw)
        self.value = int(value)
        self._max_fn = max_fn

    def GetValue(self, *_a, **_k):
        return self.value

    def GetMin(self):
        return 0

    def GetMax(self):
        return self._max_fn()

    def GetInc(self):
        return 2

    def GetUnit(self):
        return "px"

    def SetValue(self, value, *_a, **_k):
        top = self._max_fn()
        if value > top:
            # Verbatim shape of the message the rig produced.
            raise FakeSpinnakerException(
                f"Value = {value} must be equal or smaller than Max = {top}."
            )
        self.value = int(value)


class FloatNode(IntNode):
    iface = "float"

    def SetValue(self, value, *_a, **_k):
        self.value = float(value)


class BoolNode(Node):
    iface = "bool"

    def __init__(self, value, writable=True):
        super().__init__(writable)
        self.value = bool(value)

    def GetValue(self, *_a, **_k):
        return self.value

    def SetValue(self, value, *_a, **_k):
        self.value = bool(value)


class EnumEntry:
    def __init__(self, symbolic, value):
        self._symbolic = symbolic
        self._value = value
        self.readable = True
        self.writable = True

    def GetSymbolic(self):
        return self._symbolic

    def GetValue(self):
        return self._value


class EnumNode(Node):
    iface = "enum"

    def __init__(self, value, entries, writable=True):
        super().__init__(writable)
        self._entries = {name: EnumEntry(name, i) for i, name in enumerate(entries)}
        self._current = value

    def GetEntries(self):
        return list(self._entries.values())

    def GetEntryByName(self, name):
        return self._entries.get(name)  # None for an absent entry

    def SetIntValue(self, value):
        for name, entry in self._entries.items():
            if entry.GetValue() == value:
                self._current = name
                return
        raise FakeSpinnakerException(f"no entry with value {value}")

    def GetCurrentEntry(self):
        return self._entries[self._current]


class StringNode(Node):
    iface = "string"

    def __init__(self, value):
        super().__init__(writable=False)
        self.value = value

    def GetValue(self, *_a, **_k):
        return self.value


class Category(Node):
    iface = "category"

    def __init__(self, *children, visibility=BEGINNER):
        super().__init__(writable=False, visibility=visibility)
        self.children = list(children)

    def GetFeatures(self):
        return self.children


class FakeNodeMap:
    """A small SFNC node map with the ROI nodes genuinely coupled, under a
    category tree for the walk."""

    def __init__(self):
        self.nodes = {
            "Width": IntNode(SENSOR, lambda: SENSOR - self.nodes["OffsetX"].value),
            "Height": IntNode(SENSOR, lambda: SENSOR - self.nodes["OffsetY"].value),
            "OffsetX": IntNode(0, lambda: SENSOR - self.nodes["Width"].value),
            "OffsetY": IntNode(0, lambda: SENSOR - self.nodes["Height"].value),
            "ExposureTime": FloatNode(5000.0, lambda: 1e6),
            "GammaEnabled": BoolNode(False),
            "TriggerSource": EnumNode("Line0", ["Line0", "Software"]),
            "Secret": IntNode(1, lambda: 9, visibility=INVISIBLE),
            "DeviceModelName": StringNode("GS3-U3-41C6NIR"),
        }
        for name, node in self.nodes.items():
            node.name = name
        image = Category(*(self.nodes[n] for n in ("Width", "Height", "OffsetX", "OffsetY")))
        image.name = "ImageFormatControl"
        roots = [image, self.nodes["ExposureTime"], self.nodes["GammaEnabled"]]
        roots += [self.nodes["TriggerSource"], self.nodes["Secret"]]
        self.nodes["Root"] = Category(*roots)

    def GetNode(self, name):
        return self.nodes.get(name)  # absent node -> None, as GenApi does


def _identity(node):
    """The typed C*Ptr casts are identity here: GetNode returns the typed node."""
    return node


def _present(node):
    return node is not None and getattr(node, "readable", True)


def _no_stream_nodemap():
    raise FakeSpinnakerException("no TL stream node map on the fake")


FAKE_PYSPIN = SimpleNamespace(
    SpinnakerException=FakeSpinnakerException,
    IsAvailable=lambda node: node is not None,
    IsReadable=_present,
    IsWritable=lambda node: node is not None and getattr(node, "writable", True),
    CIntegerPtr=_identity,
    CFloatPtr=_identity,
    CBooleanPtr=_identity,
    CEnumerationPtr=_identity,
    CEnumEntryPtr=_identity,
    CStringPtr=_identity,
    CCommandPtr=_identity,
    CCategoryPtr=_identity,
    **{f"intfI{t}": k for t, k in (
        ("Integer", "int"), ("Float", "float"), ("Boolean", "bool"),
        ("Enumeration", "enum"), ("String", "string"), ("Command", "command"),
        ("Category", "category"),
    )},
    Beginner=BEGINNER,
    Expert=EXPERT,
    Guru=GURU,
    Invisible=INVISIBLE,
)


def _fake_cam(stream_nodes, serial="17475185"):
    """A PySpin CameraPtr stand-in; ``stream_nodes`` None has no TL stream map."""
    nodemap = FakeNodeMap()
    cam = SimpleNamespace(initialized=False, streaming=False)
    cam.Init = lambda: setattr(cam, "initialized", True)
    cam.DeInit = lambda: setattr(cam, "initialized", False)
    cam.IsInitialized = lambda: cam.initialized
    cam.IsStreaming = lambda: cam.streaming
    cam.BeginAcquisition = lambda: setattr(cam, "streaming", True)
    cam.EndAcquisition = lambda: setattr(cam, "streaming", False)
    cam.GetNodeMap = lambda: nodemap
    cam.GetTLDeviceNodeMap = lambda: SimpleNamespace(
        GetNode=lambda _name: StringNode(serial)
    )
    cam.GetUniqueID = lambda: "17475185"
    cam.GetTLStreamNodeMap = (
        _no_stream_nodemap
        if stream_nodes is None
        else lambda: SimpleNamespace(GetNode=stream_nodes.get)
    )
    return cam


@pytest.fixture
def make_backend(monkeypatch):
    monkeypatch.setattr(flir, "PySpin", FAKE_PYSPIN)

    def make(stream_nodes=None):
        b = flir.FlirBackend(flir.PySpinBinding(), _fake_cam(stream_nodes))
        assert b.serial_number == "17475185"
        b.open()
        return b

    return make


@pytest.fixture
def backend(make_backend):
    return make_backend()


def _roi_config(width, height, offset_x=0, offset_y=0):
    """A persistence TSV in the real files' node order (size before origin)."""
    return (
        "# {octacam GenApi persistence}\n"
        "# --- ImageFormatControl ---\n"
        f"Width\t{width}\n"
        f"Height\t{height}\n"
        f"OffsetX\t{offset_x}\n"
        f"OffsetY\t{offset_y}\n"
        "# --- AcquisitionControl ---\n"
        "TriggerSource\tLine0\n"
    )


# ------------------------------------------------- typed-setter exception contract
def test_set_number_out_of_range_raises_backend_error(backend):
    """A value the device refuses must surface as BackendError, not a raw
    SpinnakerException: the config applier guards itself with `except
    BackendError`, so a leak here aborts the whole rig init on one bad node."""
    backend.set_node("Height", "int", 1408)  # crop, making room for an origin
    backend.set_node("OffsetY", "int", 278)
    with pytest.raises(BackendError, match="must be equal or smaller than Max"):
        backend.set_node("Height", "int", SENSOR)  # 2048 > 2048-278


def test_set_enum_missing_entry_raises_backend_error(backend):
    with pytest.raises(BackendError):
        backend.set_node("TriggerSource", "enum", "NoSuchEntry")


def test_setters_on_an_absent_node_raise_backend_error(backend):
    for call in (
        lambda: backend.set_node("NotANode", "int", 1),
        lambda: backend.set_node("NotANode", "bool", True),
        lambda: backend.set_node("NotANode", "enum", "Off"),
    ):
        with pytest.raises(BackendError):
            call()


def test_getters_on_an_absent_node_return_none(backend):
    assert backend.get_node("NotANode", "int") is None
    assert backend.get_node("NotANode", "bool") is None
    assert backend.get_node("NotANode", "enum") is None


# ------------------------------------------------------------- ROI apply order
def test_load_params_grows_roi_past_a_stale_offset(backend):
    """The rig crash: a cropped/offset ROI left by a *previous* session clamped
    the next config's full-sensor Height (2048 against a max of 2048-278=1770).
    The applier must clear the origin before programming the size."""
    backend.load_params(_roi_config(SENSOR, 1408, offset_y=278))
    assert (backend.width(), backend.height()) == (SENSOR, 1408)
    assert backend.get_node("OffsetY", "int") == 278

    # A second rig config wanting the whole sensor back must now apply cleanly.
    backend.load_params(_roi_config(SENSOR, SENSOR))
    assert (backend.width(), backend.height()) == (SENSOR, SENSOR)
    assert backend.get_node("OffsetY", "int") == 0


def test_load_params_keeps_the_configs_own_offset(backend):
    """Clearing the origin is a pre-pass, not a policy: the file's own Offset*
    lines still land (they follow the size lines in file order)."""
    backend.load_params(_roi_config(1024, 1024, offset_x=512, offset_y=256))
    assert (backend.width(), backend.height()) == (1024, 1024)
    assert backend.get_node("OffsetX", "int") == 512
    assert backend.get_node("OffsetY", "int") == 256


def test_load_params_skips_one_bad_node_and_applies_the_rest(backend):
    """Best-effort: an unsettable node is logged and skipped, not fatal."""
    text = _roi_config(SENSOR, SENSOR) + "ExposureTime\t2500\nNotANode\t7\n"
    backend.load_params(text)  # must not raise
    assert backend.get_node("ExposureTime", "float") == 2500.0
    assert backend._original_trigger_source == "Line0"


def test_save_params_round_trips_the_roi(backend):
    backend.load_params(_roi_config(1024, 1024, offset_x=512, offset_y=256))
    values = dict(parse_config(backend.save_params()))
    assert values["Width"] == "1024"
    assert values["Height"] == "1024"
    assert values["OffsetX"] == "512"
    assert values["OffsetY"] == "256"


def test_save_params_stamps_the_device_model(backend):
    assert "# Device = GS3-U3-41C6NIR (17475185)" in backend.save_params()


# ------------------------------------------------------------- node-map walk
def test_the_walk_labels_features_by_their_categorys_display_name(backend):
    features = {f.name: f for f in backend.list_features()}
    assert "Secret" not in features  # invisible
    assert features["Width"].category == "ImageFormatControl (display)"
    assert features["ExposureTime"].category == "Other"  # directly under Root
    width = features["Width"]
    assert (width.type, width.value, width.min, width.max, width.inc, width.unit) == (
        "int", SENSOR, 0, SENSOR, 2, "px"
    )
    assert width.display_name == "Width (display)" and width.tooltip == "About Width"
    trigger = features["TriggerSource"]
    assert trigger.type == "enum" and trigger.value == "Line0"
    assert [e["value"] for e in trigger.entries] == ["Line0", "Software"]
    assert features["GammaEnabled"].type == "bool"


def test_write_feature_snaps_an_int_and_sets_an_enum_by_its_entry(backend):
    backend.write_feature("Width", 1023)  # increment 2 from min 0
    assert backend.get_node("Width", "int") == 1024
    backend.write_feature("TriggerSource", "Software")
    assert backend.read_feature("TriggerSource").value == "Software"
    with pytest.raises(BackendError):
        backend.write_feature("TriggerSource", "Bogus")
    with pytest.raises(BackendError, match="no such node"):
        backend.read_feature("Bogus")


# ------------------------------------------------------------------ session
class CamList:
    def __init__(self, cams):
        self.cams = cams
        self.cleared = 0

    def GetSize(self):
        return len(self.cams)

    def GetByIndex(self, i):
        return self.cams[i]

    def Clear(self):
        self.cleared += 1


def test_a_session_hands_out_the_requested_cameras_and_releases_the_system_last(
    monkeypatch,
):
    cam_list = CamList([_fake_cam(None, "B"), _fake_cam(None, "A")])
    system = SimpleNamespace(GetCameras=lambda: cam_list, released=0)
    system.ReleaseInstance = lambda: setattr(system, "released", system.released + 1)
    monkeypatch.setattr(
        flir,
        "PySpin",
        SimpleNamespace(**vars(FAKE_PYSPIN), System=SimpleNamespace(GetInstance=lambda: system)),
    )
    binding = flir.PySpinBinding()
    assert [serial for serial, _cam in binding.enumerate(None)] == ["A", "B"]
    # A re-enumeration tears the previous session down first.
    [(serial, cam)] = binding.enumerate(["B"])
    assert serial == "B" and cam_list.cleared == 1 and system.released == 1
    backend = flir.FlirBackend(binding, cam)
    backend.open()
    backend.close()
    assert not cam.initialized and binding._outstanding == {}
    binding.teardown()
    assert cam_list.cleared == 2 and system.released == 2 and binding._system is None


# ------------------------------------------------------------- frame fetch
class FakeImage:
    def __init__(self, incomplete=False, array=None):
        self.incomplete = incomplete
        self.array = array
        self.released = 0

    def IsIncomplete(self):
        return self.incomplete

    def GetTimeStamp(self):
        return 7

    def GetNDArray(self):
        return self.array

    def Release(self):
        self.released += 1


def _grab(backend, monkeypatch, image, *, record=False):
    """Start a (native-free) grab and return a free-run fetch of ``image``."""
    monkeypatch.setattr(
        backend, "_begin_acquisition", lambda *a, **k: backend.trigger.begin_grab()
    )
    if record:
        backend.start_grab_record()
    else:
        backend.start_grab_preview()
    backend._cam.GetNextImage = lambda _timeout: image
    return lambda wants_array=False: backend.retrieve_freerun(100, lambda: wants_array)


def test_a_non_mono8_frame_is_dropped_and_released(backend, monkeypatch):
    # A 2-D uint16 frame (the Mono8 set failed) passes an ndim check alone and
    # would be recorded as garbage; the itemsize guard drops it.
    image = FakeImage(array=np.zeros((4, 4), dtype=np.uint16))
    fetch = _grab(backend, monkeypatch, image)
    assert fetch(wants_array=True) is None
    assert image.released == 1


def test_a_mono8_frame_is_copied_with_its_timestamp(backend, monkeypatch):
    image = FakeImage(array=np.zeros((4, 4), dtype=np.uint8))
    fetch = _grab(backend, monkeypatch, image)
    array, timestamp = fetch(wants_array=True)
    assert array.dtype == np.uint8 and array is not image.array and timestamp == 7
    assert image.released == 1
    assert backend.last_trigger_index is None  # a free-run frame answers no trigger


class CommandNode:
    def __init__(self, refusals=0):
        self.refusals = refusals
        self.executed = 0

    def Execute(self):
        if self.refusals:
            self.refusals -= 1
            raise FakeSpinnakerException("TriggerSoftware is not writable")
        self.executed += 1


def _software_grab(backend, monkeypatch, image, refusals=0):
    """A started record grab whose TriggerSoftware node counts its executions."""
    _grab(backend, monkeypatch, image, record=True)
    command = CommandNode(refusals)
    backend._cam.GetNodeMap().nodes["TriggerSoftware"] = command
    return command


def test_a_software_retrieve_fires_one_trigger_and_its_image_answers_it(
    backend, monkeypatch
):
    image = FakeImage(array=np.zeros((4, 4), dtype=np.uint8))
    command = _software_grab(backend, monkeypatch, image)
    backend.trigger_once()
    array, timestamp = backend.retrieve(100, lambda: True)
    assert command.executed == 1 and array.dtype == np.uint8 and timestamp == 7
    assert backend.trigger.pending == 0 and backend.trigger.fired_index is None
    assert backend.last_trigger_index == 0 and image.released == 1


def test_an_incomplete_software_image_answers_its_trigger(backend, monkeypatch):
    # A transport failure is an incomplete image: it answers its trigger at
    # once, so the next one fires rather than waiting out the answer deadline.
    image = FakeImage(incomplete=True)
    command = _software_grab(backend, monkeypatch, image)
    backend.trigger_once()
    assert backend.retrieve(100, lambda: True) is None
    assert backend.trigger.fired_index is None and backend.last_trigger_index == 0
    assert command.executed == 1 and image.released == 1


def test_a_refused_software_trigger_is_not_awaited(backend, monkeypatch):
    image = FakeImage(array=np.zeros((4, 4), dtype=np.uint8))
    command = _software_grab(backend, monkeypatch, image, refusals=1)
    backend.trigger_once()
    assert backend.retrieve(100, lambda: True) is None
    assert backend.trigger.fired_index is None and image.released == 0
    backend.trigger_once()  # fires at once: nothing awaits trigger 0's image
    assert backend.retrieve(100, lambda: True) is not None
    assert command.executed == 1 and backend.last_trigger_index == 1


def test_close_mid_grab_ends_it_and_retrieve_stays_quiet(backend, monkeypatch):
    _software_grab(backend, monkeypatch, FakeImage())
    backend.close()
    assert backend.is_grabbing() is False
    backend.trigger.begin_grab()  # a stop race: the hand-off still reads grabbing
    backend.trigger_once()
    assert backend.retrieve(10, lambda: True) is None
    assert backend.retrieve_freerun(10, lambda: True) is None


# ------------------------------------------------------ incomplete-image log


def _incomplete_logs(records):
    return [(r.levelno, r.getMessage()) for r in records if "incomplete" in r.getMessage()]


def test_incomplete_images_are_counted_but_logged_once_per_grab(backend, monkeypatch, caplog):
    # A saturated bus delivers incomplete images continuously: every one is
    # counted, but a record grab logs only its first until the report interval.
    caplog.set_level(logging.DEBUG, logger="octacam")
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 1e9)
    fetch = _grab(backend, monkeypatch, FakeImage(incomplete=True), record=True)
    assert all(fetch() is None for _ in range(250))
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 250}
    logs = _incomplete_logs(caplog.records)
    assert len(logs) == 1 and logs[0][0] == logging.WARNING
    # A new grab logs its own first one again; the total runs on.
    fetch = _grab(backend, monkeypatch, FakeImage(incomplete=True), record=True)
    assert fetch() is None and fetch() is None
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 252}
    assert len(_incomplete_logs(caplog.records)) == 2


def test_incomplete_images_in_a_preview_log_at_debug(backend, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="octacam")
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 0.0)
    fetch = _grab(backend, monkeypatch, FakeImage(incomplete=True))
    for _ in range(5):
        fetch()
    logs = _incomplete_logs(caplog.records)
    assert logs and all(level == logging.DEBUG for level, _ in logs)
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 5}


def test_incomplete_image_reports_carry_the_grabs_running_total(backend, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="octacam")
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 0.0)  # report each
    fetch = _grab(backend, monkeypatch, FakeImage(incomplete=True), record=True)
    for _ in range(3):
        fetch()
    messages = [m for _, m in _incomplete_logs(caplog.records)]
    assert "delivered an incomplete image" in messages[0]
    assert "2 incomplete images discarded in this grab (1 since" in messages[1]
    assert "3 incomplete images discarded in this grab (1 since" in messages[2]


# ---------------------------------------------------------- acquisition start
class RefusedEnumNode(EnumNode):
    def SetIntValue(self, value):
        raise FakeSpinnakerException("node is not writable")


def _count_starts(backend):
    started = []
    backend._cam.BeginAcquisition = lambda: started.append(True)
    return started


def test_record_starts_although_its_mode_and_buffer_handling_are_refused(make_backend):
    """Only BeginAcquisition is fatal: the camera defaults to Continuous and the
    buffer handling only tunes the stream. The fake node map has no
    AcquisitionMode, and its stream refuses the buffer handling but still takes
    the record buffer count."""
    stream = {
        "StreamBufferHandlingMode": RefusedEnumNode(
            "NewestOnly", ["NewestOnly", "OldestFirst"]
        ),
        "StreamBufferCountMode": EnumNode("Auto", ["Auto", "Manual"]),
        "StreamBufferCountManual": IntNode(10, lambda: 1000),
    }
    backend = make_backend(stream)
    started = _count_starts(backend)
    assert backend.start_grab_record() is True
    assert started == [True] and backend.is_grabbing()
    assert stream["StreamBufferCountManual"].value == RECORD_STREAM_BUFFERS


def _record_stream(make_backend):
    """A backend whose stream takes a record pool, and its buffer-count node."""
    stream = {
        "StreamBufferHandlingMode": EnumNode("NewestOnly", ["NewestOnly", "OldestFirst"]),
        "StreamBufferCountMode": EnumNode("Auto", ["Auto", "Manual"]),
        "StreamBufferCountManual": IntNode(10, lambda: 1000),
    }
    return make_backend(stream), stream["StreamBufferCountManual"]


def test_a_record_pool_the_usb_memory_cannot_hold_is_halved(make_backend, caplog):
    """Two full-sensor GS3s need more usbfs memory at 128 buffers than the
    kernel's 1000 MB: BeginAcquisition refuses, and a smaller pool starts."""
    backend, count = _record_stream(make_backend)

    def begin():
        if count.value > 32:
            raise FakeSpinnakerException("Could not start acquisition. [-1001]")
        backend._cam.streaming = True

    backend._cam.BeginAcquisition = begin
    assert backend.start_grab_record() is True
    assert count.value == 32 and backend.is_grabbing()
    retries = [r for r in caplog.records if "usbfs_memory_mb" in r.getMessage()]
    assert [r.levelno for r in retries] == [logging.WARNING] * 2  # 128 -> 64 -> 32


def test_a_record_start_refused_at_every_pool_size_fails(make_backend):
    backend, count = _record_stream(make_backend)

    def refuse():
        raise FakeSpinnakerException("Could not start acquisition. [-1001]")

    backend._cam.BeginAcquisition = refuse
    with pytest.raises(BackendError, match="Could not start acquisition"):
        backend.start_grab_record()
    assert count.value == MIN_STREAM_BUFFERS and not backend.is_grabbing()


def test_preview_starts_without_a_stream_node_map(backend):
    started = _count_starts(backend)  # the fixture's camera has no stream node map
    backend.start_grab_preview()
    assert started == [True] and backend.is_grabbing()


def test_a_refused_begin_acquisition_fails_the_start(backend):
    def refuse():
        raise FakeSpinnakerException("insufficient system resources")

    backend._cam.BeginAcquisition = refuse
    with pytest.raises(BackendError, match="insufficient system resources"):
        backend.start_grab_preview()
    assert not backend.is_grabbing()
