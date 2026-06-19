"""2-photon trigger plugin: wire format, hooks, REST endpoints, broadcast."""

import struct
import threading

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
        device="/dev/ArduinoCam",
        default_fps=DEFAULT_FPS,
        default_duration_ms=DEFAULT_DURATION_MS,
    )
    link = FakeLink(is_open=is_open)
    plugin._link = link
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


def test_on_recording_start_uses_defaults_when_no_params():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(None)
    written = link.snapshot()
    assert len(written) == 1
    _, fps, dur = struct.unpack(ARM_FORMAT, written[0])
    assert fps == DEFAULT_FPS
    assert dur == DEFAULT_DURATION_MS


def test_on_recording_start_uses_defaults_when_plugin_key_absent():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"arduino": {"n_steps": 100}})  # different plugin
    written = link.snapshot()
    assert len(written) == 1
    _, fps, _ = struct.unpack(ARM_FORMAT, written[0])
    assert fps == DEFAULT_FPS


def test_on_recording_stop_abort_sends_cancel():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_stop(aborted=True)
    written = link.snapshot()
    assert written == [bytes([CANCEL_MAGIC])]


def test_on_recording_stop_clean_does_not_send_cancel():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_stop(aborted=False)
    assert link.snapshot() == []


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
    assert received == [("twophoton_state", {"state": "armed", "device": "/dev/ArduinoCam"})]

    received.clear()
    plugin._on_arduino_status("T")
    assert received[0] == ("twophoton_state", {"state": "triggered", "device": "/dev/ArduinoCam"})


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
    assert s["device"] == "/dev/ArduinoCam"
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
    assert data["device"] == "/dev/ArduinoCam"
    assert data["arduino_state"] == "idle"


def test_reconnect_endpoint_reopens_link(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: None)  # suppress real serial open
    link._open = True   # simulate successful open
    client = _test_client(plugin)
    r = client.post("/api/twophoton/reconnect")
    assert r.status_code == 200
    assert r.json()["device"] == "/dev/ArduinoCam"


# ---------------------------------------------------------------------------
# Factory: _build
# ---------------------------------------------------------------------------

def test_build_requires_device():
    with pytest.raises(RuntimeError, match="device"):
        _build({})


def test_build_raises_without_pyserial(monkeypatch):
    import octacam.plugins.twophoton as m
    monkeypatch.setattr(m, "serial", None)
    with pytest.raises(RuntimeError, match="pyserial"):
        _build({"device": "/dev/ArduinoCam"})


def test_build_uses_provided_options():
    plugin = _build(
        {"device": "/dev/ArduinoCam", "baud": 9600, "default_fps": 50, "default_duration_ms": 3000}
    )
    assert plugin.device == "/dev/ArduinoCam"
    assert plugin.baud == 9600
    assert plugin._default_fps == 50
    assert plugin._default_duration_ms == 3000
