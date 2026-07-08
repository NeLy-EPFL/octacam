"""harvesters backend node/param mapping, with a mocked ImageAcquirer.

No GenTL producer or hardware: the backend's ImageAcquirer is replaced by a fake
node map so the SFNC → NodeInfo mapping, param round-trip, triggering, frame
reshape, and the bounded close are exercised in pure Python.
"""

import os
import types

import genicam.genapi as genapi
import numpy as np

from octacam.cameras import harvesters as hv
from octacam.cameras._genicam_config import parse_config
from octacam.cameras.harvesters import HarvestersBackend, _find_cti_files


class FakeNode:
    """A genicam IInteger/IFloat-like node with bounds and an access mode."""

    def __init__(self, value, mn=None, mx=None, inc=None, unit=None, writable=True):
        self._value = value
        self.min = mn
        self.max = mx
        self.inc = inc
        self.unit = unit
        self._writable = writable

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = v

    def get_access_mode(self):
        return genapi.EAccessMode.RW if self._writable else genapi.EAccessMode.RO


class FakeEnum:
    """A genicam IEnumeration-like node (symbolic string value)."""

    def __init__(self, value):
        self.value = value


class FakeCommand:
    def __init__(self):
        self.executed = 0

    def execute(self):
        self.executed += 1


class FakeNodeMap:
    def __init__(self):
        self.Width = FakeNode(1920, mn=16, mx=1920, inc=16, unit="px")
        self.Height = FakeNode(1200, mn=16, mx=1200, inc=8, unit="px")
        self.OffsetX = FakeNode(0, mn=0, mx=1904, inc=4, unit="px")
        self.OffsetY = FakeNode(0, mn=0, mx=1184, inc=2, unit="px")
        self.ExposureTime = FakeNode(5000.0, mn=20.0, mx=1e6, inc=1.0, unit="us")
        self.Gain = FakeNode(1.5, mn=0.0, mx=24.0, inc=0.1, unit="dB")
        self.TriggerSource = FakeEnum("Line1")
        self.TriggerSelector = FakeEnum("FrameStart")
        self.TriggerMode = FakeEnum("Off")
        self.TriggerOverlap = FakeEnum("Off")
        self.PixelFormat = FakeEnum("Mono8")
        self.TriggerSoftware = FakeCommand()


class FakeBuffer:
    def __init__(self, component, timestamp_ns):
        self.payload = types.SimpleNamespace(components=[component])
        self.timestamp_ns = timestamp_ns
        self.queued = False

    def queue(self):
        self.queued = True


class FakeIA:
    def __init__(self, node_map):
        self.remote_device = types.SimpleNamespace(node_map=node_map)
        self._acquiring = False
        self.destroyed = False
        self._buffer = None

    def is_acquiring(self):
        return self._acquiring

    def start(self):
        self._acquiring = True

    def stop(self):
        self._acquiring = False

    def try_fetch(self, *, timeout=0):
        return self._buffer

    def destroy(self):
        self.destroyed = True


def _backend(node_map=None):
    backend = HarvestersBackend("H-1")
    backend._ia = FakeIA(node_map or FakeNodeMap())
    return backend


def test_read_node_maps_bounds_and_writability():
    backend = _backend()
    assert backend.serial_number == "H-1" and backend.is_open()
    width = backend.read_node("width")
    assert width.value == 1920 and width.min == 16 and width.max == 1920
    assert width.inc == 16 and width.unit == "px" and width.writable is True
    exposure = backend.read_node("exposure")
    assert exposure.value == 5000.0 and exposure.unit == "us"


def test_writability_reflects_access_mode():
    nm = FakeNodeMap()
    nm.Gain = FakeNode(1.5, writable=False)
    backend = _backend(nm)
    assert backend.read_node("gain").writable is False


def test_write_node_routes_to_int_or_float():
    nm = FakeNodeMap()
    backend = _backend(nm)
    backend.write_node("width", 800)
    assert nm.Width.value == 800 and isinstance(nm.Width.value, int)
    backend.write_node("exposure", 1234.5)
    assert nm.ExposureTime.value == 1234.5


def test_params_round_trip():
    nm = FakeNodeMap()
    backend = _backend(nm)
    # Native GenApi persistence TSV: tab-separated feature lines, applied in order.
    backend.load_params(
        "# GenApi persistence file\nExposureTime\t2222.0\nWidth\t640\n"
    )
    assert nm.ExposureTime.value == 2222.0 and nm.Width.value == 640
    values = dict(parse_config(backend.save_params()))
    assert values["ExposureTime"] == "2222"  # _fmt_float drops the trailing .0
    assert values["Width"] == "640"
    assert values["TriggerSource"] == "Line1"  # captured original


def test_trigger_once_is_a_pure_bump_not_a_device_call():
    # The device TriggerSoftware.execute() now happens in retrieve() on the grab
    # thread, NOT in trigger_once() on the shared timer thread — so one camera's
    # slow trigger can never block another's. trigger_once only bumps the counter.
    nm = FakeNodeMap()
    backend = _backend(nm)
    backend.begin_software_trigger_preview()
    assert nm.TriggerSource.value == "Software" and nm.TriggerMode.value == "On"
    # TriggerOverlap=ReadOut lets triggers pipeline during readout — without it the
    # FLIR ignores every other software trigger (~halved fps; measured 4.7->64 fps).
    assert nm.TriggerOverlap.value == "ReadOut"
    backend.start_grab_preview()
    assert backend.is_grabbing()
    backend.trigger_once()
    assert nm.TriggerSoftware.executed == 0  # NOT fired yet — retrieve fires it
    assert backend._pending == 1
    backend.stop_grab()
    assert not backend.is_grabbing()


def test_retrieve_fires_trigger_reshapes_and_requeues():
    nm = FakeNodeMap()
    backend = _backend(nm)
    component = types.SimpleNamespace(
        data=np.arange(6, dtype=np.uint8), width=3, height=2
    )
    buffer = FakeBuffer(component, timestamp_ns=123)
    backend._ia._buffer = buffer
    backend.start_grab_preview()
    backend.trigger_once()  # arm one pending frame
    frame = backend.retrieve(100, lambda: True)
    assert frame is not None
    array, timestamp = frame
    assert timestamp == 123 and array.shape == (2, 3)
    assert buffer.queued  # returned to the producer pool
    assert nm.TriggerSoftware.executed == 1  # retrieve fired the device trigger
    assert backend._pending == 0  # consumed


def test_retrieve_none_without_a_pending_trigger():
    # No trigger armed: retrieve must not fire the device or block indefinitely.
    nm = FakeNodeMap()
    backend = _backend(nm)
    backend.start_grab_preview()
    assert backend.retrieve(1, lambda: True) is None
    assert nm.TriggerSoftware.executed == 0


def test_retrieve_none_on_no_buffer():
    nm = FakeNodeMap()
    backend = _backend(nm)
    backend._ia._buffer = None
    backend.start_grab_preview()
    backend.trigger_once()
    assert backend.retrieve(1, lambda: True) is None
    assert nm.TriggerSoftware.executed == 1  # trigger fired; frame just didn't arrive


def test_close_is_bounded_and_idempotent():
    ia = FakeIA(FakeNodeMap())
    backend = HarvestersBackend("H-1")
    backend._ia = ia
    backend.close()
    assert not backend.is_open() and ia.destroyed
    backend.close()  # second close is a no-op


def test_find_cti_uses_env(monkeypatch, tmp_path):
    producer = tmp_path / "mvGenTLProducer.cti"
    producer.write_text("")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    monkeypatch.delenv("GENICAM_GENTL32_PATH", raising=False)
    monkeypatch.delenv("OCTACAM_GENTL_CTI", raising=False)
    monkeypatch.delenv("OCTACAM_GENTL_PRODUCER", raising=False)
    assert str(producer) in _find_cti_files()


def _seed_producers(monkeypatch, tmp_path, *names):
    for name in names:
        (tmp_path / name).write_text("")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    monkeypatch.delenv("GENICAM_GENTL32_PATH", raising=False)
    monkeypatch.delenv("OCTACAM_GENTL_CTI", raising=False)


def test_find_cti_denies_spinnaker_by_default(monkeypatch, tmp_path):
    _seed_producers(monkeypatch, tmp_path, "Spinnaker_GenTL.cti", "mvGenTLProducer.cti")
    monkeypatch.delenv("OCTACAM_GENTL_PRODUCER", raising=False)
    got = [os.path.basename(f) for f in _find_cti_files()]
    assert "mvGenTLProducer.cti" in got
    assert "Spinnaker_GenTL.cti" not in got  # known GIL-deadlock; denied by default


def test_producer_env_allowlists_and_orders(monkeypatch, tmp_path):
    _seed_producers(
        monkeypatch,
        tmp_path,
        "Spinnaker_GenTL.cti",
        "mvGenTLProducer.cti",
        "VimbaUSBTL.cti",
    )
    monkeypatch.setenv("OCTACAM_GENTL_PRODUCER", "Vimba" + os.pathsep + "mvGenTL")
    got = [os.path.basename(f) for f in _find_cti_files()]
    # allowlist (Spinnaker dropped) applied in the requested priority order
    assert got == ["VimbaUSBTL.cti", "mvGenTLProducer.cti"]


def test_producer_env_can_force_a_denied_producer(monkeypatch, tmp_path):
    _seed_producers(monkeypatch, tmp_path, "Spinnaker_GenTL.cti", "mvGenTLProducer.cti")
    monkeypatch.setenv("OCTACAM_GENTL_PRODUCER", "Spinnaker")
    got = [os.path.basename(f) for f in _find_cti_files()]
    assert got == ["Spinnaker_GenTL.cti"]  # explicit request overrides the deny


class FakeDeviceInfo:
    def __init__(self, serial):
        self.serial_number = serial


class FakeHarvester:
    """Minimal Harvester stand-in mirroring the real create() ambiguity rule."""

    def __init__(self, infos):
        self.device_info_list = infos
        self.created = None

    def update(self):
        pass

    def create(self, search_key):
        if isinstance(search_key, dict):  # a bare serial dict: real API raises on 2+
            serial = search_key.get("serial_number")
            matches = [i for i in self.device_info_list if i.serial_number == serial]
            if len(matches) != 1:
                raise ValueError(
                    "multiple devices found: provide sufficient search key"
                )
            self.created = matches[0]
        else:  # a specific DeviceInfo object: unambiguous
            self.created = search_key
        return FakeIA(FakeNodeMap())


def test_open_binds_to_specific_producer_when_serial_is_ambiguous(monkeypatch):
    # Same camera enumerated by two producers (as with Spinnaker + mvIMPACT).
    infos = [FakeDeviceInfo("17475185"), FakeDeviceInfo("17475185")]
    fake = FakeHarvester(infos)
    monkeypatch.setattr(hv, "_get_harvester", lambda: fake)
    backend = HarvestersBackend("17475185")
    backend.open()  # must not raise the GenTL "multiple devices found" error
    assert backend.is_open()
    assert fake.created is infos[0]  # bound to the first (highest-priority) match


def test_enumerate_dedups_serials_seen_by_multiple_producers(monkeypatch):
    infos = [FakeDeviceInfo("A"), FakeDeviceInfo("A"), FakeDeviceInfo("B")]
    monkeypatch.setattr(hv, "_get_harvester", lambda: FakeHarvester(infos))
    assert hv.enumerate_harvesters() == [("A", "A"), ("B", "B")]


# --- per-device offset grab-lock (harvesters serves any vendor) ------------


def test_grab_locked_defaults_to_size_only_for_unknown_vendor():
    backend = _backend()  # open() not called, so the vendor is unknown
    assert backend.grab_locked_features() == frozenset({"Width", "Height"})


def test_grab_locked_includes_offsets_for_basler():
    backend = _backend()
    backend._vendor = "Basler acA1920-40um"
    assert {"Width", "Height", "OffsetX", "OffsetY"} <= backend.grab_locked_features()


def test_grab_locked_is_size_only_for_flir():
    backend = _backend()
    backend._vendor = "FLIR"
    assert backend.grab_locked_features() == frozenset({"Width", "Height"})


def test_read_vendor_name_drives_offset_grab_lock():
    node_map = FakeNodeMap()
    node_map.DeviceVendorName = FakeEnum("Basler")
    backend = _backend(node_map)
    backend._vendor = backend._read_vendor_name()
    assert backend._vendor == "Basler"
    assert {"OffsetX", "OffsetY"} <= backend.grab_locked_features()
