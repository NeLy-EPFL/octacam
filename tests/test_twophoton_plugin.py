"""2-photon trigger plugin: wire format, hooks, REST endpoints, broadcast."""

import queue
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from octacam.plugins.twophoton import (
    DEFAULT_DURATION_MS,
    DEFAULT_FPS,
    ArmParams,
    TwoPhotonPlugin,
    _build,
)

# Wire-format constants (mirror the firmware and plugin source)
ARM_MAGIC    = 0xA5
CANCEL_MAGIC = 0xCA
ARM_FORMAT   = "<BHI"  # magic(u8) + fps(u16) + duration_ms(u32) = 7 bytes


# ---------------------------------------------------------------------------
# FakeLink — stands in for TwoPhotonLink without any serial port
# ---------------------------------------------------------------------------

class FakeLink:
    """Records writes; lets tests inject incoming status bytes."""

    def __init__(self, is_open: bool = True):
        self._open = is_open
        self._lock = threading.Lock()
        self.written: list[bytes] = []

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self, device, baud) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def send_arm(self, params: ArmParams) -> None:
        with self._lock:
            if self._open:
                self.written.append(params.to_bytes())

    def send_cancel(self) -> None:
        with self._lock:
            if self._open:
                self.written.append(bytes([CANCEL_MAGIC]))

    def snapshot(self) -> list[bytes]:
        with self._lock:
            return list(self.written)


def _plugin_with_fake(is_open: bool = True) -> tuple[TwoPhotonPlugin, FakeLink]:
    """Return a TwoPhotonPlugin whose link is replaced with a FakeLink."""
    plugin = TwoPhotonPlugin(
        device="/dev/arduinoCams",
        default_fps=DEFAULT_FPS,
        default_duration_ms=DEFAULT_DURATION_MS,
    )
    link = FakeLink(is_open=is_open)
    plugin._link = link
    # FakeLink has no reader thread to send the 'A' ack, so skip the bounded
    # ack wait in on_recording_start (exercised separately with a real link).
    plugin._ack_timeout_s = 0.0
    return plugin, link


# ---------------------------------------------------------------------------
# ArmParams: wire format
# ---------------------------------------------------------------------------

def test_arm_params_to_bytes_correct_format():
    params = ArmParams(fps=100, duration_ms=10_000)
    raw = params.to_bytes()
    assert len(raw) == 7
    magic, fps, duration_ms = struct.unpack(ARM_FORMAT, raw)
    assert magic == ARM_MAGIC
    assert fps == 100
    assert duration_ms == 10_000


def test_arm_params_to_bytes_boundary_values():
    params = ArmParams(fps=1, duration_ms=1)
    magic, fps, dur = struct.unpack(ARM_FORMAT, params.to_bytes())
    assert magic == ARM_MAGIC
    assert fps == 1
    assert dur == 1

    params = ArmParams(fps=10_000, duration_ms=2**32 - 1)
    magic, fps, dur = struct.unpack(ARM_FORMAT, params.to_bytes())
    assert fps == 10_000
    assert dur == 2**32 - 1


def test_arm_params_from_payload_uses_provided_values():
    params = ArmParams.from_payload({"fps": 50, "duration_ms": 5000}, 100, 10_000)
    assert params.fps == 50
    assert params.duration_ms == 5000


def test_arm_params_from_payload_falls_back_to_defaults():
    params = ArmParams.from_payload({}, 75, 7500)
    assert params.fps == 75
    assert params.duration_ms == 7500


def test_arm_params_from_payload_clamps_fps():
    p_low  = ArmParams.from_payload({"fps": 0}, 100, 1000)
    p_high = ArmParams.from_payload({"fps": 99_999}, 100, 1000)
    assert p_low.fps == 1       # clamped to min
    assert p_high.fps == 10_000 # clamped to max


def test_arm_params_from_payload_clamps_duration():
    p = ArmParams.from_payload({"duration_ms": -5}, 100, 1000)
    assert p.duration_ms == 1   # clamped to min


def test_arm_params_from_payload_handles_invalid_types():
    params = ArmParams.from_payload({"fps": "bad", "duration_ms": None}, 80, 8000)
    assert params.fps == 80
    assert params.duration_ms == 8000


# ---------------------------------------------------------------------------
# Plugin: recording lifecycle hooks
# ---------------------------------------------------------------------------

def test_on_recording_start_sends_arm_packet():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"twophoton": {"fps": 120, "duration_ms": 5000}})
    written = link.snapshot()
    assert len(written) == 1
    magic, fps, dur = struct.unpack(ARM_FORMAT, written[0])
    assert magic == ARM_MAGIC
    assert fps == 120
    assert dur == 5000


def test_on_recording_start_does_not_arm_when_no_params():
    # params=None means "arm with recording" was unchecked — must not send anything.
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(None)
    assert link.snapshot() == []


def test_on_recording_start_does_not_arm_when_plugin_key_absent():
    # A different plugin's params present but no "twophoton" key → do not arm.
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"flywheel": {"n_steps": 100}})
    assert link.snapshot() == []


def test_on_recording_start_uses_defaults_when_twophoton_key_present_but_empty():
    # {"twophoton": {}} means checkbox was checked, GUI omitted optional fields.
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"twophoton": {}})
    written = link.snapshot()
    assert len(written) == 1
    _, fps, dur = struct.unpack(ARM_FORMAT, written[0])
    assert fps == DEFAULT_FPS
    assert dur == DEFAULT_DURATION_MS


def test_on_recording_stop_abort_sends_cancel():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_stop(aborted=True)
    written = link.snapshot()
    assert written == [bytes([CANCEL_MAGIC])]


def test_on_recording_stop_clean_also_cancels_and_resets():
    # A manual early stop arrives with aborted=False while the firmware may still
    # be RUNNING, so a clean stop must also cancel the hardware trigger (and reset
    # local state); cancelling an already-IDLE board is a harmless no-op.
    plugin, link = _plugin_with_fake()
    plugin._arduino_state = "triggered"
    plugin.on_recording_stop(aborted=False)
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]
    assert plugin._arduino_state == "idle"


def test_on_recording_start_silently_skips_when_port_closed():
    plugin, link = _plugin_with_fake(is_open=False)
    plugin.on_recording_start(None)   # must not raise
    assert link.snapshot() == []


# ---------------------------------------------------------------------------
# Plugin: Arduino status callback and broadcast
# ---------------------------------------------------------------------------

def test_arduino_status_updates_internal_state():
    plugin, _ = _plugin_with_fake()
    assert plugin._arduino_state == "idle"
    plugin._on_arduino_status("A")
    assert plugin._arduino_state == "armed"
    plugin._on_arduino_status("T")
    assert plugin._arduino_state == "triggered"
    plugin._on_arduino_status("D")
    assert plugin._arduino_state == "done"


def test_broadcast_called_on_status_change():
    plugin, _ = _plugin_with_fake()
    received = []
    plugin.set_broadcast(lambda kind, payload: received.append((kind, payload)))

    plugin._on_arduino_status("A")
    assert received == [
        ("twophoton_state", {"state": "armed", "device": "/dev/arduinoCams", "ready": True})
    ]

    received.clear()
    plugin._on_arduino_status("T")
    assert received[0] == (
        "twophoton_state",
        {"state": "triggered", "device": "/dev/arduinoCams", "ready": True},
    )


def test_no_broadcast_when_callback_not_set():
    plugin, _ = _plugin_with_fake()
    plugin._on_arduino_status("A")   # must not raise even without a broadcast hook


# ---------------------------------------------------------------------------
# Plugin: is_ready / status
# ---------------------------------------------------------------------------

def test_is_ready_reflects_link_state():
    plugin, link = _plugin_with_fake(is_open=True)
    assert plugin.is_ready() is True
    link._open = False
    assert plugin.is_ready() is False


def test_status_includes_device_and_state():
    plugin, _ = _plugin_with_fake()
    plugin._on_arduino_status("A")
    s = plugin.status()
    assert s["device"] == "/dev/arduinoCams"
    assert s["arduino_state"] == "armed"


# ---------------------------------------------------------------------------
# Plugin: REST endpoints
# ---------------------------------------------------------------------------

def _test_client(plugin: TwoPhotonPlugin) -> TestClient:
    app = FastAPI()
    router = plugin.api_router()
    app.include_router(router)
    return TestClient(app)


def test_get_status_endpoint():
    plugin, _ = _plugin_with_fake()
    client = _test_client(plugin)
    r = client.get("/api/twophoton/status")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["device"] == "/dev/arduinoCams"
    assert data["arduino_state"] == "idle"


def test_reconnect_endpoint_reopens_link(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: None)  # suppress real serial open
    link._open = True   # simulate successful open
    client = _test_client(plugin)
    r = client.post("/api/twophoton/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["device"] == "/dev/arduinoCams"
    # The endpoint exists to report the post-reopen state — assert it, not just
    # the static device field.
    assert data["ready"] is True
    assert data["error"] is None


def test_reconnect_endpoint_surfaces_failure(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    # _open returns the error string and leaves the link closed (port absent).
    monkeypatch.setattr(plugin, "_open", lambda: "could not open /dev/arduinoCams")
    client = _test_client(plugin)
    r = client.post("/api/twophoton/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is False
    assert data["error"] == "could not open /dev/arduinoCams"


def test_from_payload_clamps_duration_to_uint32():
    # An out-of-range duration must clamp, not blow up to_bytes() with struct.error.
    p = ArmParams.from_payload({"duration_ms": 2**40}, 100, 10_000)
    assert p.duration_ms == 0xFFFF_FFFF
    assert len(p.to_bytes()) == 7  # still packs cleanly


def test_on_recording_start_warns_and_skips_when_link_closed():
    import logging

    # Attach directly to the octacam logger rather than via caplog: another test
    # may leave propagate=False, which would empty caplog's root-level capture.
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        plugin, link = _plugin_with_fake(is_open=False)
        plugin.on_recording_start({"twophoton": {"fps": 100, "duration_ms": 5000}})
    finally:
        logger.removeHandler(handler)
    assert link.snapshot() == []  # nothing armed
    assert any("is not open" in r.getMessage() for r in records)  # warning emitted


def test_on_recording_stop_aborted_resets_state_and_broadcasts():
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    plugin.set_broadcast(lambda topic, data: events.append((topic, data)))
    plugin._arduino_state = "triggered"
    plugin.on_recording_stop(aborted=True)
    # Firmware goes IDLE silently on cancel; the host must reset+broadcast itself.
    assert plugin._arduino_state == "idle"
    assert (
        "twophoton_state",
        {"state": "idle", "device": "/dev/arduinoCams", "ready": True},
    ) in events
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]


def _capture_octacam_logs():
    """Attach a handler to the octacam logger; returns (records, detach)."""
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    return records, lambda: logger.removeHandler(handler)


def test_on_recording_start_warns_when_no_arm_ack():
    # With no reader to send 'A', the bounded ack wait elapses and warns rather
    # than letting a silently-dropped arm leave the cameras hanging.
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._ack_timeout_s = 0.05
        plugin.on_recording_start({"twophoton": {"fps": 100, "duration_ms": 1000}})
    finally:
        detach()
    assert link.snapshot()  # the arm packet was still sent
    assert any("no arm acknowledgement" in r.getMessage() for r in records)


def test_on_recording_start_no_warning_when_ack_arrives():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._ack_timeout_s = 1.0

        def ack():
            # Deliver the firmware 'A' as soon as the arm packet is written.
            for _ in range(500):
                if link.snapshot():
                    plugin._on_arduino_status("A")
                    return
                time.sleep(0.001)

        t = threading.Thread(target=ack)
        t.start()
        plugin.on_recording_start({"twophoton": {"fps": 100, "duration_ms": 1000}})
        t.join(timeout=2.0)
    finally:
        detach()
    assert plugin._armed_event.is_set()
    assert not any("no arm acknowledgement" in r.getMessage() for r in records)


def test_link_broken_broadcasts_not_ready():
    # When the reader marks the port broken mid-session, the plugin must push a
    # state with ready=False so the GUI disables the arm gate.
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    plugin.set_broadcast(lambda topic, data: events.append((topic, data)))
    link._open = False  # reader saw the port die
    plugin._on_link_broken()
    assert events[-1] == (
        "twophoton_state",
        {"state": "idle", "device": "/dev/arduinoCams", "ready": False},
    )


# ---------------------------------------------------------------------------
# Factory: _build
# ---------------------------------------------------------------------------

def test_build_uses_default_device_when_omitted():
    # No device key → falls back to DEFAULT_DEVICE ("/dev/arduinoCams")
    from octacam.plugins.twophoton import DEFAULT_DEVICE
    plugin = _build({})
    assert plugin.device == DEFAULT_DEVICE


def test_build_raises_without_pyserial(monkeypatch):
    import octacam.plugins.twophoton as m
    monkeypatch.setattr(m, "serial", None)
    with pytest.raises(RuntimeError, match="pyserial"):
        _build({"device": "/dev/arduinoCams"})


def test_build_uses_provided_options():
    plugin = _build(
        {"device": "/dev/arduinoCams", "baud": 9600, "default_fps": 50, "default_duration_ms": 3000}
    )
    assert plugin.device == "/dev/arduinoCams"
    assert plugin.baud == 9600
    assert plugin._default_fps == 50
    assert plugin._default_duration_ms == 3000


# ---------------------------------------------------------------------------
# TwoPhotonLink: is_open thread-safety
# ---------------------------------------------------------------------------

def test_is_open_safe_when_serial_is_none():
    # Regression: is_open must not raise AttributeError when _serial is nulled
    # concurrently by close(). Snapshot the attribute to a local first.
    from octacam.plugins.twophoton import TwoPhotonLink
    link = TwoPhotonLink(on_status=lambda s: None)
    # _serial starts as None; is_open should return False without AttributeError.
    assert link.is_open is False


# ---------------------------------------------------------------------------
# TwoPhotonLink: real link over a fake serial port (open/close/read/write).
#
# These exercise the actual TwoPhotonLink serial plumbing (reader thread,
# write path, close-join) that the FakeLink-based tests above bypass.
# ---------------------------------------------------------------------------


class _FakeSerialError(Exception):
    """Stand-in for serial.SerialException."""


class _FakeSerial:
    """Minimal pyserial-Serial double: records writes, hands out queued reads."""

    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.written = bytearray()
        self._reads: queue.Queue[bytes] = queue.Queue()
        self.raise_on_read: Exception | None = None

    def write(self, data) -> int:
        self.written += bytes(data)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        if self.raise_on_read is not None:
            exc, self.raise_on_read = self.raise_on_read, None
            raise exc
        try:
            return self._reads.get(timeout=0.05)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.is_open = False

    def feed(self, data: bytes) -> None:
        self._reads.put(data)


def _fake_serial_ns(fake: _FakeSerial) -> SimpleNamespace:
    return SimpleNamespace(
        Serial=lambda *a, **k: fake, SerialException=_FakeSerialError
    )


def _wait(pred, timeout=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_link_open_starts_reader_and_close_joins(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.TwoPhotonLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    assert link._reader is not None and link._reader.is_alive()
    link.close()
    assert link.is_open is False
    assert link._reader is None


def test_link_send_arm_writes_packet(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.TwoPhotonLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    params = m.ArmParams(fps=120, duration_ms=5000)
    link.send_arm(params)
    link.close()
    assert bytes(fake.written) == params.to_bytes()


def test_link_reader_invokes_status_callback(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    got: list[str] = []
    link = m.TwoPhotonLink(on_status=got.append)
    link.open("/dev/fake", 115200)
    fake.feed(b"A")
    fake.feed(b"T")
    fake.feed(b"Z")  # not a status byte -> ignored
    assert _wait(lambda: got == ["A", "T"])
    link.close()


def test_link_read_error_marks_broken(monkeypatch):
    """A mid-run port failure must flip is_open False so the GUI offers reconnect."""
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.TwoPhotonLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    fake.raise_on_read = _FakeSerialError("device disconnected")
    assert _wait(lambda: link.is_open is False)
    link.close()  # still safe / idempotent after the link broke


def test_link_close_swallows_shutdown_read_error(monkeypatch):
    """Reader hitting an error during close() exits quietly (no crash, joined)."""
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.TwoPhotonLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    # Mimics os.read(None, 1) raising TypeError when the port is nulled under us.
    fake.raise_on_read = TypeError("os.read(None, 1)")
    link.close()
    assert link._reader is None
    assert link.is_open is False


def test_link_reopen_starts_fresh_reader(monkeypatch):
    """open -> close -> open must start a live reader. The reused _reader_stop
    Event must be cleared so the second reader is not born already-stopped."""
    import octacam.plugins.twophoton as m

    created: list[_FakeSerial] = []

    def make_serial(*a, **k):
        fake = _FakeSerial()
        created.append(fake)
        return fake

    monkeypatch.setattr(
        m,
        "serial",
        SimpleNamespace(Serial=make_serial, SerialException=_FakeSerialError),
    )
    got: list[str] = []
    link = m.TwoPhotonLink(on_status=got.append)
    link.open("/dev/fake", 115200)
    link.close()
    link.open("/dev/fake", 115200)  # reopen reuses the same _reader_stop Event
    assert link.is_open is True
    assert link._reader is not None and link._reader.is_alive()
    created[-1].feed(b"A")  # the second port's reader must still process bytes
    assert _wait(lambda: got == ["A"])
    link.close()


def test_link_broken_invokes_on_broken_callback(monkeypatch):
    """A mid-run read failure fires the on_broken hook so the owner can surface
    the lost link (a clean close must NOT fire it)."""
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    broken: list[bool] = []
    link = m.TwoPhotonLink(on_status=lambda s: None, on_broken=lambda: broken.append(True))
    link.open("/dev/fake", 115200)
    fake.raise_on_read = _FakeSerialError("device disconnected")
    assert _wait(lambda: broken == [True])
    link.close()


# ---------------------------------------------------------------------------
# Plugin registry: builtins always win over external entry points
# ---------------------------------------------------------------------------

def test_builtin_not_overridden_by_entry_point(monkeypatch):
    """An external entry-point named 'twophoton' must not replace the builtin."""
    import octacam.plugins as registry_mod
    from octacam.plugins import _REGISTRY

    sentinel_factory = lambda opts: object()  # noqa: E731

    class FakeEP:
        name = "twophoton"
        value = "fake_package:factory"
        def load(self):
            return sentinel_factory

    def fake_entry_points(group):
        return [FakeEP()]

    # Clear twophoton from registry so the entry-point would normally win.
    saved = _REGISTRY.pop("twophoton", None)
    try:
        monkeypatch.setattr(
            "octacam.plugins.entry_points", fake_entry_points, raising=False
        )
        # Patch importlib.metadata.entry_points inside the module
        import importlib.metadata as meta_mod
        monkeypatch.setattr(meta_mod, "entry_points", fake_entry_points)

        registry_mod._discover_entry_points()

        # The builtin name must not have been replaced by the external factory.
        assert _REGISTRY.get("twophoton") is not sentinel_factory, (
            "External entry-point overwrote the builtin 'twophoton' factory"
        )
    finally:
        if saved is not None:
            _REGISTRY["twophoton"] = saved
