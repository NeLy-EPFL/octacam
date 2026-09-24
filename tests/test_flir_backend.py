"""FlirBackend's typed-setter seam and config applier, with PySpin faked.

No Spinnaker SDK or hardware: the module-level ``PySpin`` binding (the only thing
this backend touches the SDK through) is replaced by a fake whose nodes model the
one GenICam behaviour that matters here — a ROI *size* node's max is
``sensor - origin``, so a stale origin makes a full-sensor Width/Height write out
of range. That is the constraint the rig hit (``Value = 2048 must be equal or
smaller than Max = 1770`` on Height), and it is reproduced here in pure Python.
"""

import logging
from types import SimpleNamespace

import pytest

import octacam.cameras.flir as flir
from octacam.cameras._genicam_config import parse_config
from octacam.cameras.base import BackendError

SENSOR = 2048


# --------------------------------------------------------------------- fakes
class FakeSpinnakerException(Exception):
    """Stands in for PySpin.SpinnakerException (a GenICam OutOfRangeException)."""


class IntNode:
    """An integer node whose ceiling is computed live, as GenApi's is."""

    def __init__(self, value, max_fn, writable=True):
        self.value = int(value)
        self._max_fn = max_fn
        self.writable = writable
        self.readable = True

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
    def SetValue(self, value, *_a, **_k):
        self.value = float(value)


class BoolNode:
    def __init__(self, value, writable=True):
        self.value = bool(value)
        self.writable = writable
        self.readable = True

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


class EnumNode:
    def __init__(self, value, entries, writable=True):
        self.writable = writable
        self.readable = True
        self._entries = {name: EnumEntry(name, i) for i, name in enumerate(entries)}
        self._current = value

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


class StringNode:
    def __init__(self, value):
        self.value = value
        self.readable = True
        self.writable = False

    def GetValue(self, *_a, **_k):
        return self.value


class FakeNodeMap:
    """A small SFNC node map with the ROI nodes genuinely coupled."""

    def __init__(self):
        self.nodes = {
            "Width": IntNode(SENSOR, lambda: SENSOR - self.nodes["OffsetX"].value),
            "Height": IntNode(SENSOR, lambda: SENSOR - self.nodes["OffsetY"].value),
            "OffsetX": IntNode(0, lambda: SENSOR - self.nodes["Width"].value),
            "OffsetY": IntNode(0, lambda: SENSOR - self.nodes["Height"].value),
            "ExposureTime": FloatNode(5000.0, lambda: 1e6),
            "GammaEnabled": BoolNode(False),
            "TriggerSource": EnumNode("Line0", ["Line0", "Software"]),
        }

    def GetNode(self, name):
        return self.nodes.get(name)  # absent node -> None, as GenApi does


def _identity(node):
    """The typed C*Ptr casts are identity here: GetNode returns the typed node."""
    return node


def _present(node):
    return node is not None and getattr(node, "readable", True)


def _no_stream_nodemap():
    raise FakeSpinnakerException("no TL stream node map on the fake")


@pytest.fixture
def backend(monkeypatch):
    nodemap = FakeNodeMap()
    monkeypatch.setattr(
        flir,
        "PySpin",
        SimpleNamespace(
            SpinnakerException=FakeSpinnakerException,
            IsAvailable=lambda node: node is not None,
            IsReadable=_present,
            IsWritable=lambda node: (
                node is not None and getattr(node, "writable", True)
            ),
            CIntegerPtr=_identity,
            CFloatPtr=_identity,
            CBooleanPtr=_identity,
            CEnumerationPtr=_identity,
            CStringPtr=_identity,
        ),
    )
    cam = SimpleNamespace(
        GetNodeMap=lambda: nodemap,
        GetTLDeviceNodeMap=lambda: SimpleNamespace(
            GetNode=lambda _name: StringNode("17475185")
        ),
        GetUniqueID=lambda: "17475185",
        IsInitialized=lambda: True,
        GetTLStreamNodeMap=_no_stream_nodemap,
    )
    b = flir.FlirBackend(cam)
    assert b.serial_number == "17475185"
    return b


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
    backend._set_number("Height", 1408, True)  # crop, making room for an origin
    backend._set_number("OffsetY", 278, True)
    with pytest.raises(BackendError, match="must be equal or smaller than Max"):
        backend._set_number("Height", SENSOR, True)  # 2048 > 2048-278


def test_set_enum_missing_entry_raises_backend_error(backend):
    with pytest.raises(BackendError):
        backend._set_enum("TriggerSource", "NoSuchEntry")


def test_setters_on_an_absent_node_raise_backend_error(backend):
    for call in (
        lambda: backend._set_number("NotANode", 1, True),
        lambda: backend._set_bool("NotANode", True),
        lambda: backend._set_enum("NotANode", "Off"),
    ):
        with pytest.raises(BackendError):
            call()


def test_getters_on_an_absent_node_return_none(backend):
    assert backend._get_number("NotANode", True) is None
    assert backend._get_bool("NotANode") is None
    assert backend._get_enum("NotANode") is None


# ------------------------------------------------------------- ROI apply order
def test_load_params_grows_roi_past_a_stale_offset(backend):
    """The rig crash: a cropped/offset ROI left by a *previous* session clamped
    the next config's full-sensor Height (2048 against a max of 2048-278=1770).
    The applier must clear the origin before programming the size."""
    backend.load_params(_roi_config(SENSOR, 1408, offset_y=278))
    assert (backend.width(), backend.height()) == (SENSOR, 1408)
    assert backend._get_number("OffsetY", True) == 278

    # A second rig config wanting the whole sensor back must now apply cleanly.
    backend.load_params(_roi_config(SENSOR, SENSOR))
    assert (backend.width(), backend.height()) == (SENSOR, SENSOR)
    assert backend._get_number("OffsetY", True) == 0


def test_load_params_keeps_the_configs_own_offset(backend):
    """Clearing the origin is a pre-pass, not a policy: the file's own Offset*
    lines still land (they follow the size lines in file order)."""
    backend.load_params(_roi_config(1024, 1024, offset_x=512, offset_y=256))
    assert (backend.width(), backend.height()) == (1024, 1024)
    assert backend._get_number("OffsetX", True) == 512
    assert backend._get_number("OffsetY", True) == 256


def test_load_params_skips_one_bad_node_and_applies_the_rest(backend):
    """Best-effort: an unsettable node is logged and skipped, not fatal."""
    text = _roi_config(SENSOR, SENSOR) + "ExposureTime\t2500\nNotANode\t7\n"
    backend.load_params(text)  # must not raise
    assert backend._get_number("ExposureTime", False) == 2500.0
    assert backend._original_trigger_source == "Line0"


def test_save_params_round_trips_the_roi(backend):
    backend.load_params(_roi_config(1024, 1024, offset_x=512, offset_y=256))
    values = dict(parse_config(backend.save_params()))
    assert values["Width"] == "1024"
    assert values["Height"] == "1024"
    assert values["OffsetX"] == "512"
    assert values["OffsetY"] == "256"


# ------------------------------------------------------ incomplete-image log
class FakeImage:
    def __init__(self, incomplete=True):
        self.incomplete = incomplete
        self.released = False

    def IsIncomplete(self):
        return self.incomplete

    def GetTimeStamp(self):
        return 7

    def Release(self):
        self.released = True


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def octacam_log():
    """Every record the octacam logger emits, debug included."""
    handler = _Records()
    logger = logging.getLogger("octacam")
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield handler.records
    logger.removeHandler(handler)
    logger.setLevel(level)


def _grab(backend, monkeypatch, *, record):
    """Start a (native-free) grab and return a fetch of one incomplete image."""
    monkeypatch.setattr(backend, "_begin_acquisition", lambda *args, **kwargs: None)
    if record:
        backend.start_grab_record()
    else:
        backend.start_grab_preview()
    cam = SimpleNamespace(GetNextImage=lambda _timeout: FakeImage())
    return lambda: backend._fetch_image(cam, 100, lambda: False)


def _incomplete_logs(records):
    return [(r.levelno, r.getMessage()) for r in records if "incomplete" in r.getMessage()]


def test_incomplete_images_are_counted_but_logged_once_per_grab(backend, monkeypatch, octacam_log):
    # A saturated bus delivers incomplete images continuously: every one is
    # counted, but a record grab logs only its first until the report interval.
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 1e9)
    fetch = _grab(backend, monkeypatch, record=True)
    assert all(fetch() is None for _ in range(250))
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 250}
    logs = _incomplete_logs(octacam_log)
    assert len(logs) == 1 and logs[0][0] == logging.WARNING
    # A new grab logs its own first one again; the total runs on.
    fetch = _grab(backend, monkeypatch, record=True)
    assert fetch() is None and fetch() is None
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 252}
    assert len(_incomplete_logs(octacam_log)) == 2


def test_incomplete_images_in_a_preview_log_at_debug(backend, monkeypatch, octacam_log):
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 0.0)
    fetch = _grab(backend, monkeypatch, record=False)
    for _ in range(5):
        fetch()
    logs = _incomplete_logs(octacam_log)
    assert logs and all(level == logging.DEBUG for level, _ in logs)
    assert backend.stream_statistics() == {"IncompleteImagesDiscarded": 5}


def test_incomplete_image_reports_carry_the_grabs_running_total(backend, monkeypatch, octacam_log):
    monkeypatch.setattr(flir, "INCOMPLETE_REPORT_INTERVAL_S", 0.0)  # report each
    fetch = _grab(backend, monkeypatch, record=True)
    for _ in range(3):
        fetch()
    messages = [m for _, m in _incomplete_logs(octacam_log)]
    assert "delivered an incomplete image" in messages[0]
    assert "2 incomplete images discarded in this grab (1 since" in messages[1]
    assert "3 incomplete images discarded in this grab (1 since" in messages[2]
