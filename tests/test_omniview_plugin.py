"""omniview trigger + strobe plugin: wire format, hooks, REST, broadcast, link."""

import queue
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from octacam.plugins.omniview import (
    DEFAULT_CAM_PULSE_US,
    DEFAULT_DURATION_MS,
    DEFAULT_DUTY_PERCENT,
    DEFAULT_FPS,
    ArmParams,
    OmniviewPlugin,
    _build,
)

# Wire-format constants (mirror the firmware and plugin source)
ARM_MAGIC = 0xA5
CANCEL_MAGIC = 0xCA
# magic(u8) + fps(u16) + duration_ms(u32) + duty_permille(u16) + cam_pulse_us(u16)
ARM_FORMAT = "<BHIHH"  # = 11 bytes
DEVICE = "/dev/ttyACM0"


# ---------------------------------------------------------------------------
# FakeLink — stands in for OmniviewLink without any serial port
# ---------------------------------------------------------------------------


class FakeLink:
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


def _plugin_with_fake(is_open: bool = True) -> tuple[OmniviewPlugin, FakeLink]:
    plugin = OmniviewPlugin(
        device=DEVICE,
        default_fps=DEFAULT_FPS,
        default_duration_ms=DEFAULT_DURATION_MS,
        default_duty_percent=DEFAULT_DUTY_PERCENT,
        default_cam_pulse_us=DEFAULT_CAM_PULSE_US,
    )
    link = FakeLink(is_open=is_open)
    plugin._link = link
    # FakeLink has no reader thread to send the 'R' ack, so skip the bounded ack
    # wait in on_recording_start (exercised separately with a real link).
    plugin._ack_timeout_s = 0.0
    return plugin, link


def _unpack(raw: bytes):
    return struct.unpack(ARM_FORMAT, raw)


# ---------------------------------------------------------------------------
# ArmParams: wire format
# ---------------------------------------------------------------------------


def test_arm_params_to_bytes_correct_format():
    params = ArmParams(fps=80, duration_ms=10_000, duty_permille=200, cam_pulse_us=500)
    raw = params.to_bytes()
    assert len(raw) == 11
    magic, fps, dur, duty, cam = _unpack(raw)
    assert (magic, fps, dur, duty, cam) == (ARM_MAGIC, 80, 10_000, 200, 500)


def test_arm_params_to_bytes_boundary_values():
    magic, fps, dur, duty, cam = _unpack(
        ArmParams(fps=1, duration_ms=1, duty_permille=0, cam_pulse_us=0).to_bytes()
    )
    assert (magic, fps, dur, duty, cam) == (ARM_MAGIC, 1, 1, 0, 0)
    _, fps, dur, duty, cam = _unpack(
        ArmParams(
            fps=5000, duration_ms=2**32 - 1, duty_permille=1000, cam_pulse_us=65535
        ).to_bytes()
    )
    assert (fps, dur, duty, cam) == (5000, 2**32 - 1, 1000, 65535)


def test_from_payload_uses_provided_values():
    p = ArmParams.from_payload(
        {"fps": 60, "duration_ms": 5000, "duty_percent": 35, "cam_pulse_us": 300},
        DEFAULT_FPS,
        DEFAULT_DURATION_MS,
        DEFAULT_DUTY_PERCENT,
        DEFAULT_CAM_PULSE_US,
    )
    assert p.fps == 60
    assert p.duration_ms == 5000
    assert p.duty_permille == 350  # 35% -> 350 permille
    assert p.cam_pulse_us == 300


def test_from_payload_falls_back_to_defaults():
    p = ArmParams.from_payload({}, 75, 7500, 12.5, 250)
    assert p.fps == 75
    assert p.duration_ms == 7500
    assert p.duty_permille == 125  # 12.5% -> 125 permille
    assert p.cam_pulse_us == 250


def test_from_payload_clamps_fps():
    lo = ArmParams.from_payload({"fps": 0}, 80, 1000, 20, 0)
    hi = ArmParams.from_payload({"fps": 99_999}, 80, 1000, 20, 0)
    assert lo.fps == 1
    assert hi.fps == 5000


def test_from_payload_clamps_duty():
    lo = ArmParams.from_payload({"duty_percent": -5}, 80, 1000, 20, 0)
    hi = ArmParams.from_payload({"duty_percent": 150}, 80, 1000, 20, 0)
    assert lo.duty_permille == 0
    assert hi.duty_permille == 1000  # clamped to 100% -> 1000 permille


def test_from_payload_clamps_duration_to_uint32():
    p = ArmParams.from_payload({"duration_ms": 2**40}, 80, 10_000, 20, 0)
    assert p.duration_ms == 0xFFFF_FFFF
    assert len(p.to_bytes()) == 11  # still packs cleanly


def test_from_payload_handles_invalid_types():
    p = ArmParams.from_payload(
        {"fps": "bad", "duration_ms": None, "duty_percent": "x", "cam_pulse_us": None},
        80,
        8000,
        20.0,
        100,
    )
    assert p.fps == 80
    assert p.duration_ms == 8000
    assert p.duty_permille == 200  # default 20%
    assert p.cam_pulse_us == 100


# ---------------------------------------------------------------------------
# Plugin: recording lifecycle hooks
# ---------------------------------------------------------------------------


def test_on_recording_start_sends_arm_packet():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(
        {"omniview": {"fps": 80, "duration_ms": 5000, "duty_percent": 20}}
    )
    written = link.snapshot()
    assert len(written) == 1
    magic, fps, dur, duty, _ = _unpack(written[0])
    assert (magic, fps, dur, duty) == (ARM_MAGIC, 80, 5000, 200)


def test_on_recording_start_does_not_arm_when_no_params():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(None)
    assert link.snapshot() == []


def test_on_recording_start_does_not_arm_when_plugin_key_absent():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"flywheel": {"n_steps": 100}})
    assert link.snapshot() == []


def test_on_recording_start_uses_defaults_when_key_present_but_empty():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"omniview": {}})
    written = link.snapshot()
    assert len(written) == 1
    _, fps, dur, duty, cam = _unpack(written[0])
    assert fps == DEFAULT_FPS
    assert dur == DEFAULT_DURATION_MS
    assert duty == int(round(DEFAULT_DUTY_PERCENT * 10))
    assert cam == DEFAULT_CAM_PULSE_US


def test_on_recording_stop_abort_sends_cancel():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_stop(aborted=True)
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]


def test_on_recording_stop_clean_also_cancels_and_resets():
    plugin, link = _plugin_with_fake()
    plugin._arduino_state = "running"
    plugin.on_recording_stop(aborted=False)
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]
    assert plugin._arduino_state == "idle"


def test_on_recording_start_silently_skips_when_port_closed():
    plugin, link = _plugin_with_fake(is_open=False)
    plugin.on_recording_start({"omniview": {"fps": 80}})  # must not raise
    assert link.snapshot() == []


def test_on_recording_start_warns_and_skips_when_link_closed():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake(is_open=False)
        plugin.on_recording_start({"omniview": {"fps": 80, "duration_ms": 5000}})
    finally:
        detach()
    assert link.snapshot() == []
    assert any("is not open" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# Plugin: status callback and broadcast
# ---------------------------------------------------------------------------


def test_status_token_updates_internal_state():
    plugin, _ = _plugin_with_fake()
    assert plugin._arduino_state == "idle"
    plugin._on_arduino_status("R")
    assert plugin._arduino_state == "running"
    plugin._on_arduino_status("D")
    assert plugin._arduino_state == "done"
    plugin._on_arduino_status("C")
    assert plugin._arduino_state == "idle"


def test_broadcast_called_on_status_change():
    plugin, _ = _plugin_with_fake()
    received: list[tuple[str, dict]] = []
    plugin.set_broadcast(lambda kind, payload: received.append((kind, payload)))
    plugin._on_arduino_status("R")
    assert received == [
        ("omniview_state", {"state": "running", "device": DEVICE, "ready": True})
    ]


def test_no_broadcast_when_callback_not_set():
    plugin, _ = _plugin_with_fake()
    plugin._on_arduino_status("R")  # must not raise without a broadcast hook


def test_running_token_releases_ack_event():
    plugin, _ = _plugin_with_fake()
    plugin._armed_event.clear()
    plugin._on_arduino_status("R")
    assert plugin._armed_event.is_set()


def test_link_broken_broadcasts_not_ready():
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    plugin.set_broadcast(lambda topic, data: events.append((topic, data)))
    link._open = False
    plugin._on_link_broken()
    assert events[-1] == (
        "omniview_state",
        {"state": "idle", "device": DEVICE, "ready": False},
    )


# ---------------------------------------------------------------------------
# Plugin: is_ready / status
# ---------------------------------------------------------------------------


def test_is_ready_reflects_link_state():
    plugin, link = _plugin_with_fake(is_open=True)
    assert plugin.is_ready() is True
    link._open = False
    assert plugin.is_ready() is False


def test_status_includes_device_state_and_duty():
    plugin, _ = _plugin_with_fake()
    plugin._on_arduino_status("R")
    s = plugin.status()
    assert s["device"] == DEVICE
    assert s["arduino_state"] == "running"
    assert s["duty_percent"] == DEFAULT_DUTY_PERCENT


# ---------------------------------------------------------------------------
# Plugin: REST endpoints
# ---------------------------------------------------------------------------


def _test_client(plugin: OmniviewPlugin) -> TestClient:
    app = FastAPI()
    app.include_router(plugin.api_router())
    return TestClient(app)


def test_get_status_endpoint():
    plugin, _ = _plugin_with_fake()
    r = _test_client(plugin).get("/api/omniview/status")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["device"] == DEVICE
    assert data["arduino_state"] == "idle"
    assert data["duty_percent"] == DEFAULT_DUTY_PERCENT


def test_reconnect_endpoint_reopens_link(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: None)
    link._open = True
    r = _test_client(plugin).post("/api/omniview/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["error"] is None


def test_reconnect_endpoint_surfaces_failure(monkeypatch):
    plugin, _ = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: "could not open /dev/ttyACM0")
    r = _test_client(plugin).post("/api/omniview/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is False
    assert data["error"] == "could not open /dev/ttyACM0"


# ---------------------------------------------------------------------------
# Plugin: arm acknowledgement wait
# ---------------------------------------------------------------------------


def _capture_octacam_logs():
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    return records, lambda: logger.removeHandler(handler)


def test_on_recording_start_warns_when_no_arm_ack():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._ack_timeout_s = 0.05
        plugin.on_recording_start({"omniview": {"fps": 80, "duration_ms": 1000}})
    finally:
        detach()
    assert link.snapshot()  # the arm packet was still sent
    assert any("no run acknowledgement" in r.getMessage() for r in records)


def test_on_recording_start_no_warning_when_ack_arrives():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._ack_timeout_s = 1.0

        def ack():
            for _ in range(500):
                if link.snapshot():
                    plugin._on_arduino_status("R")
                    return
                time.sleep(0.001)

        t = threading.Thread(target=ack)
        t.start()
        plugin.on_recording_start({"omniview": {"fps": 80, "duration_ms": 1000}})
        t.join(timeout=2.0)
    finally:
        detach()
    assert plugin._armed_event.is_set()
    assert not any("no run acknowledgement" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# Factory: _build
# ---------------------------------------------------------------------------


def test_build_uses_default_device_when_omitted():
    from octacam.plugins.omniview import DEFAULT_DEVICE

    assert _build({}).device == DEFAULT_DEVICE


def test_build_raises_without_pyserial(monkeypatch):
    import octacam.plugins.omniview as m

    monkeypatch.setattr(m, "serial", None)
    with pytest.raises(RuntimeError, match="pyserial"):
        _build({"device": DEVICE})


def test_build_uses_provided_options():
    plugin = _build(
        {
            "device": DEVICE,
            "baud": 9600,
            "default_fps": 50,
            "default_duration_ms": 3000,
            "default_duty_percent": 30,
            "default_cam_pulse_us": 400,
        }
    )
    assert plugin.device == DEVICE
    assert plugin.baud == 9600
    assert plugin._default_fps == 50
    assert plugin._default_duration_ms == 3000
    assert plugin._default_duty_percent == 30
    assert plugin._default_cam_pulse_us == 400


# ---------------------------------------------------------------------------
# OmniviewLink over a fake serial port (line-based reader)
# ---------------------------------------------------------------------------


class _FakeSerialError(Exception):
    pass


class _FakeSerial:
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
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.OmniviewLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    assert link._reader is not None and link._reader.is_alive()
    link.close()
    assert link.is_open is False
    assert link._reader is None


def test_link_send_arm_writes_packet(monkeypatch):
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.OmniviewLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    params = m.ArmParams(fps=80, duration_ms=5000, duty_permille=200, cam_pulse_us=0)
    link.send_arm(params)
    link.close()
    assert bytes(fake.written) == params.to_bytes()


def test_link_reader_parses_newline_tokens(monkeypatch):
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    got: list[str] = []
    link = m.OmniviewLink(on_status=got.append)
    link.open("/dev/fake", 115200)
    fake.feed(b"R\n")
    fake.feed(b"D\n")
    fake.feed(b"OMNIVIEW 1\n")  # identify line -> not a status token, ignored
    fake.feed(b"C\n")
    assert _wait(lambda: got == ["R", "D", "C"])
    link.close()


def test_link_reader_handles_split_token(monkeypatch):
    # A token split across two reads must still parse once the newline arrives.
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    got: list[str] = []
    link = m.OmniviewLink(on_status=got.append)
    link.open("/dev/fake", 115200)
    fake.feed(b"R")
    fake.feed(b"\nD\n")
    assert _wait(lambda: got == ["R", "D"])
    link.close()


def test_link_read_error_marks_broken(monkeypatch):
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = m.OmniviewLink(on_status=lambda s: None)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    fake.raise_on_read = _FakeSerialError("device disconnected")
    assert _wait(lambda: link.is_open is False)
    link.close()


def test_link_broken_invokes_on_broken_callback(monkeypatch):
    import octacam.plugins.omniview as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    broken: list[bool] = []
    link = m.OmniviewLink(
        on_status=lambda s: None, on_broken=lambda: broken.append(True)
    )
    link.open("/dev/fake", 115200)
    fake.raise_on_read = _FakeSerialError("device disconnected")
    assert _wait(lambda: broken == [True])
    link.close()


def test_is_open_safe_when_serial_is_none():
    from octacam.plugins.omniview import OmniviewLink

    link = OmniviewLink(on_status=lambda s: None)
    assert link.is_open is False


# ---------------------------------------------------------------------------
# Registry: omniview is a builtin
# ---------------------------------------------------------------------------


def test_omniview_is_registered_builtin():
    from octacam.plugins import _BUILTINS

    assert "omniview" in _BUILTINS
