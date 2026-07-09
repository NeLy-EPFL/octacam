"""triggerbox plugin (protocol v2): wire format, resolve, hooks, REST, link."""

import logging
import queue
import re
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from octacam.plugins import _ALIASES, _BUILTINS, build_plugins
from octacam.plugins.triggerbox import (
    _PROTOCOL_VERSION,
    PIN_LABELS,
    ArmSpec,
    LightChannel,
    TriggerboxLink,
    TriggerboxPlugin,
    _build,
    pin_id,
)

ARM_MAGIC = 0xA5
CANCEL_MAGIC = 0xCA
IDENTIFY_MAGIC = 0x3F
DEVICE = "/dev/ttyACM0"

_HDR = struct.Struct("<BBH")
_FIXED = struct.Struct("<HIBB")
_CAM = struct.Struct("<BHH")
_LIGHT = struct.Struct("<BBIIII")


@pytest.fixture(autouse=True)
def _no_real_usb_reset(monkeypatch):
    """Never issue a real USBDEVFS_RESET from the suite — it would reset a board
    actually plugged into the dev machine. Default to an inert failure; a test
    that exercises recovery re-monkeypatches these to simulate an outcome."""
    import octacam.serial_ports as sp

    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (False, "test: suppressed"))
    monkeypatch.setattr(sp, "wait_for_device", lambda device, timeout=3.0: True)


class _Broadcasts:
    """Capture set_broadcast() calls so a test can assert what the GUI is told."""

    def __init__(self):
        self.msgs: list[dict] = []

    def __call__(self, topic: str, payload: dict) -> None:
        assert topic == "triggerbox_state"
        self.msgs.append(payload)

    def last_error(self):
        errs = [m.get("error") for m in self.msgs if m.get("error")]
        return errs[-1] if errs else None


# ---------------------------------------------------------------------------
# Frame decoder — the reference the tests check the packer against
# ---------------------------------------------------------------------------


def _decode_frame(raw: bytes) -> dict:
    magic, version, payload_len = _HDR.unpack_from(raw, 0)
    assert magic == ARM_MAGIC
    body = raw[1 : 4 + payload_len]  # version + len + payload
    checksum = 0
    for b in body:
        checksum ^= b
    assert checksum == raw[-1], "checksum mismatch"
    assert len(raw) == 4 + payload_len + 1
    fps, duration_ms, n_cam, n_light = _FIXED.unpack_from(raw, 4)
    assert payload_len == 8 + 5 * n_cam + 18 * n_light
    off = 4 + 8
    cams = []
    for _ in range(n_cam):
        cams.append(_CAM.unpack_from(raw, off))
        off += 5
    lights = []
    for _ in range(n_light):
        lights.append(_LIGHT.unpack_from(raw, off))
        off += 18
    return {
        "version": version,
        "fps": fps,
        "duration_ms": duration_ms,
        "cams": cams,
        "lights": lights,
    }


# ---------------------------------------------------------------------------
# FakeLink — stands in for TriggerboxLink without any serial port
# ---------------------------------------------------------------------------


class FakeLink:
    def __init__(self, is_open: bool = True):
        self._open = is_open
        self._lock = threading.Lock()
        self.written: list[bytes] = []
        self.fail_writes = False  # simulate a wedged link (write returns False)
        self.opens = 0
        self.closes = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self, device, baud) -> None:
        self._open = True
        self.opens += 1

    def close(self) -> None:
        self._open = False
        self.closes += 1

    def send_arm(self, spec: ArmSpec) -> bool:
        with self._lock:
            if self._open and not self.fail_writes:
                self.written.append(spec.to_bytes())
                return True
            return False

    def send_cancel(self) -> None:
        with self._lock:
            if self._open:
                self.written.append(bytes([CANCEL_MAGIC]))

    def send_identify(self) -> None:
        pass

    def identify(self, timeout: float = 0.5):
        return getattr(self, "banner", None)

    @property
    def identity(self):
        return getattr(self, "banner", None)

    def snapshot(self) -> list[bytes]:
        with self._lock:
            return list(self.written)


class FakeCamera:
    def __init__(self, name, exposure_us, trigger_delay_us=0.0, has_delay=True):
        self.name = name
        self._exposure = exposure_us
        self._delay = trigger_delay_us
        self._has_delay = has_delay

    def read_param(self, name):
        assert name == "exposure"
        return {"value": self._exposure}

    def read_feature(self, name):
        assert name == "TriggerDelay"
        if not self._has_delay:
            raise RuntimeError("node unavailable on this model")
        return {"value": self._delay}


class FakeController:
    def __init__(self, cameras):
        self.camera_system = list(cameras)


def _plugin_with_fake(is_open: bool = True, **kwargs) -> tuple[TriggerboxPlugin, FakeLink]:
    plugin = _build({"device": DEVICE, **kwargs})
    link = FakeLink(is_open=is_open)
    plugin._link = link
    plugin._ack_timeout_s = 0.0  # no reader thread to send the 'R' ack
    return plugin, link


def _last_arm(link: FakeLink) -> dict:
    frames = [f for f in link.snapshot() if f and f[0] == ARM_MAGIC]
    assert frames, "no arm packet was sent"
    return _decode_frame(frames[-1])


# ===========================================================================
# Wire format
# ===========================================================================


def test_arm_spec_to_bytes_layout_and_checksum():
    arm = ArmSpec(
        fps=80,
        duration_ms=5000,
        cameras=[(pin_id("D13"), 500, 0)],
        lights=[(pin_id("D5"), 1, 0, 2500, 0, 0)],
    )
    raw = arm.to_bytes()
    assert len(raw) == 4 + (8 + 5 + 18) + 1
    dec = _decode_frame(raw)  # asserts checksum + length invariants internally
    assert dec["version"] == _PROTOCOL_VERSION
    assert dec["fps"] == 80 and dec["duration_ms"] == 5000
    assert dec["cams"] == [(pin_id("D13"), 500, 0)]
    assert dec["lights"] == [(pin_id("D5"), 1, 0, 2500, 0, 0)]


def test_flipping_a_byte_breaks_the_checksum():
    raw = bytearray(ArmSpec(80, 1000, [(11, 500, 0)], []).to_bytes())
    raw[5] ^= 0xFF  # corrupt a payload byte
    with pytest.raises(AssertionError, match="checksum"):
        _decode_frame(bytes(raw))


def test_empty_spec_has_only_the_fixed_prefix():
    dec = _decode_frame(ArmSpec(100, 0, [], []).to_bytes())
    assert dec["cams"] == [] and dec["lights"] == []
    assert dec["fps"] == 100 and dec["duration_ms"] == 0


def test_pin_labels_match_firmware_table():
    """The Python PIN_LABELS must stay byte-for-byte in sync with kPinTable."""
    ino = (Path(__file__).resolve().parents[1] / "arduino" / "triggerbox" / "triggerbox.ino").read_text()
    m = re.search(r"kPinTable\[kNumPins\]\s*=\s*\{([^}]*)\}", ino, re.S)
    assert m, "kPinTable not found in triggerbox.ino"
    tokens = tuple(t.strip() for t in m.group(1).replace("\n", " ").split(",") if t.strip())
    assert tokens == PIN_LABELS


def test_pin_id_maps_known_pins():
    assert pin_id("D13") == 11
    assert pin_id("D5") == 3 and pin_id("D6") == 4 and pin_id("D7") == 5
    assert pin_id("A0") == 12 and pin_id("A7") == 19
    with pytest.raises(ValueError):
        pin_id("D99")


# ===========================================================================
# LightChannel.resolve — per-mode field mapping
# ===========================================================================

PERIOD_80 = 1_000_000.0 / 80  # 12500 µs


def test_resolve_strobe_manual_duty():
    lc = LightChannel(channel=1, pin="D5", mode="strobe", duty_mode="manual", duty_percent=20)
    pid, mode, p0, p1, p2, p3 = lc.resolve(PERIOD_80, auto_led_on_us=None)
    assert (pid, mode) == (pin_id("D5"), 1)
    assert p1 == 2500  # 20% of 12500 µs
    assert (p0, p2, p3) == (0, 0, 0)


def test_resolve_strobe_auto_uses_exposure_ceil():
    lc = LightChannel(pin="D5", mode="strobe", duty_mode="auto", duty_percent=20)
    _pid, _mode, _p0, p1, *_ = lc.resolve(PERIOD_80, auto_led_on_us=2100.5)
    assert p1 == 2101  # ceil, never undershoot the exposure window


def test_resolve_strobe_auto_falls_back_to_manual_without_exposure():
    lc = LightChannel(pin="D5", mode="strobe", duty_mode="auto", duty_percent=20)
    _pid, _mode, _p0, p1, *_ = lc.resolve(PERIOD_80, auto_led_on_us=None)
    assert p1 == 2500  # falls back to the manual duty


def test_resolve_continuous_and_off():
    cont = LightChannel(pin="D6", mode="continuous").resolve(PERIOD_80, None)
    assert cont == (pin_id("D6"), 2, 0, 0, 0, 0)
    off = LightChannel(pin="D7", mode="off").resolve(PERIOD_80, None)
    assert off == (pin_id("D7"), 0, 0, 0, 0, 0)


def test_resolve_pulse_train_converts_freq_and_ms():
    lc = LightChannel(
        pin="D7", mode="pulse_train", freq_hz=10, pulse_us=5000, start_delay_ms=2, train_ms=500
    )
    pid, mode, p0, p1, p2, p3 = lc.resolve(PERIOD_80, None)
    assert (pid, mode) == (pin_id("D7"), 3)
    assert p0 == 5000        # pulse_us
    assert p1 == 100000      # 10 Hz -> 100 ms interval
    assert p2 == 2000        # 2 ms start delay
    assert p3 == 500000      # 500 ms train


# ===========================================================================
# Per-channel auto duty (server-side, reads live exposures)
# ===========================================================================


def test_auto_and_manual_channels_size_independently():
    plugin, link = _plugin_with_fake(
        lights=[
            {"channel": 1, "mode": "strobe", "duty_mode": "auto"},
            {"channel": 2, "mode": "strobe", "duty_mode": "manual", "duty_percent": 10},
        ],
        strobe_guard_us=100,
    )
    plugin.set_controller(
        FakeController([FakeCamera("a", 2000, 50), FakeCamera("b", 1000, 0)])
    )
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    lights = _last_arm(link)["lights"]
    # channel 1 auto: max(2000+50, 1000+0) + 100 = 2150 µs
    assert lights[0][3] == 2150
    # channel 2 manual: 10% of 12500 = 1250 µs
    assert lights[1][3] == 1250


def test_auto_duty_skips_camera_without_exposure_but_uses_others():
    plugin, link = _plugin_with_fake(
        lights=[{"channel": 1, "mode": "strobe", "duty_mode": "auto"}], strobe_guard_us=0
    )
    plugin.set_controller(
        FakeController([FakeCamera("a", None), FakeCamera("b", 1500, 0)])
    )
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    assert _last_arm(link)["lights"][0][3] == 1500


def test_auto_duty_reads_trigger_delay_zero_when_unavailable():
    plugin, link = _plugin_with_fake(
        lights=[{"channel": 1, "mode": "strobe", "duty_mode": "auto"}], strobe_guard_us=0
    )
    plugin.set_controller(FakeController([FakeCamera("a", 1000, 999, has_delay=False)]))
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    assert _last_arm(link)["lights"][0][3] == 1000  # delay treated as 0


def test_auto_duty_without_controller_falls_back_to_manual():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake(
            lights=[{"channel": 1, "mode": "strobe", "duty_mode": "auto", "duty_percent": 30}]
        )
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    finally:
        detach()
    assert _last_arm(link)["lights"][0][3] == round(0.30 * PERIOD_80)
    assert any("no camera exposure could be read" in r.getMessage() for r in records)


# ===========================================================================
# Factory: _build (config parsing)
# ===========================================================================


def test_build_defaults_reproduce_classic_rig():
    plugin = _build({})
    assert [c.pin for c in plugin._cameras] == ["D13"]
    assert [(lt.channel, lt.pin, lt.mode) for lt in plugin._lights] == [
        (1, "D5", "strobe"),
        (2, "D6", "strobe"),
    ]


def test_build_parses_cameras_and_lights_arrays():
    plugin = _build(
        {
            "cameras": [
                {"pin": "D13", "pulse_us": 500},
                {"pin": "D10", "pulse_us": 300, "delay_us": 5},
            ],
            "lights": [
                {"channel": 3, "mode": "pulse_train", "freq_hz": 5, "pulse_us": 1000},
            ],
        }
    )
    assert [(c.pin, c.pulse_us, c.delay_us) for c in plugin._cameras] == [
        ("D13", 500, 0),
        ("D10", 300, 5),
    ]
    assert len(plugin._lights) == 1
    assert plugin._lights[0].mode == "pulse_train" and plugin._lights[0].pin == "D7"


def test_build_accepts_legacy_duty_percent_key():
    # The old config wrote duty_percent / cam_pulse_us (no default_ prefix).
    plugin = _build({"duty_percent": 42, "cam_pulse_us": 321})
    assert plugin._default_duty_percent == 42
    assert plugin._default_cam_pulse_us == 321
    # classic-rig strobe channels inherit the configured manual duty
    assert plugin._lights[0].duty_percent == 42


def test_build_default_auto_makes_classic_channels_auto():
    plugin = _build({"default_duty_auto": True})
    assert all(lt.duty_mode == "auto" for lt in plugin._lights)


def test_build_unknown_camera_pin_falls_back(caplog):
    plugin = _build({"cameras": [{"pin": "D99"}]})
    assert plugin._cameras[0].pin == "D13"


def test_build_raises_without_pyserial(monkeypatch):
    import octacam.plugins.triggerbox as m

    monkeypatch.setattr(m, "serial", None)
    with pytest.raises(RuntimeError, match="pyserial"):
        _build({"device": DEVICE})


# ===========================================================================
# on_recording_start / stop
# ===========================================================================


def test_on_recording_start_arms_with_full_spec():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(
        {
            "triggerbox": {
                "fps": 100,
                "duration_ms": 2000,
                "cameras": [{"pin": "D13", "pulse_us": 500}],
                "lights": [{"channel": 1, "mode": "continuous"}],
            }
        }
    )
    dec = _last_arm(link)
    assert dec["fps"] == 100 and dec["duration_ms"] == 2000
    assert dec["cams"] == [(pin_id("D13"), 500, 0)]
    assert dec["lights"] == [(pin_id("D5"), 2, 0, 0, 0, 0)]


def test_on_recording_start_uses_configured_spec_when_only_fps_given():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    dec = _last_arm(link)
    assert len(dec["cams"]) == 1  # the classic D13 line
    assert len(dec["lights"]) == 2  # ch1 + ch2


def test_on_recording_start_accepts_legacy_omniview_key():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"omniview": {"fps": 80, "duration_ms": 1000}})
    assert _last_arm(link)["fps"] == 80


def test_on_recording_start_legacy_duty_overrides_strobe_channels():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(
        {"triggerbox": {"fps": 80, "duration_ms": 1000, "duty_percent": 50}}
    )
    # both classic strobe channels get 50% of 12500 = 6250 µs
    assert all(lt[3] == 6250 for lt in _last_arm(link)["lights"])


def test_on_recording_start_ignored_without_spec():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({})
    plugin.on_recording_start(None)
    assert not link.snapshot()


def test_on_recording_start_warns_and_skips_when_link_closed():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake(is_open=False)
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    finally:
        detach()
    assert not link.snapshot()
    assert any("is not open" in r.getMessage() for r in records)


def test_on_recording_start_refuses_on_incompatible_firmware():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._firmware_ok = False
        plugin._firmware = "OMNIVIEW 1"
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    finally:
        detach()
    assert not link.snapshot()
    assert any("refusing to arm" in r.getMessage() for r in records)


def test_on_recording_stop_sends_cancel():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_stop(aborted=False)
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]
    plugin.on_recording_stop(aborted=True)
    assert plugin._arduino_state == "idle"


def test_default_start_params_shape():
    plugin = _build({})
    params = plugin.default_start_params(fps=80.0, duration_s=5.0)
    assert params["fps"] == 80 and params["duration_ms"] == 5000
    assert [c["pin"] for c in params["cameras"]] == ["D13"]
    assert len(params["lights"]) == 2
    # round-trips: feeding it back into on_recording_start arms
    plugin._link = FakeLink()
    plugin._ack_timeout_s = 0.0
    plugin.on_recording_start({"triggerbox": params})
    assert _last_arm(plugin._link)["fps"] == 80


# ===========================================================================
# Arm acknowledgement + reject
# ===========================================================================


def _capture_octacam_logs():
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    return records, lambda: logger.removeHandler(handler)


def test_arm_and_wait_classifies_outcomes():
    plugin, link = _plugin_with_fake()
    plugin._ack_timeout_s = 0.0
    arm = plugin._build_arm_spec(80, 1000, plugin._cameras, plugin._lights)
    assert plugin._arm_and_wait(arm) == "ok"  # ack disabled -> optimistic ok
    link.fail_writes = True
    assert plugin._arm_and_wait(arm) == "write_failed"
    link.fail_writes = False
    plugin._ack_timeout_s = 0.02
    assert plugin._arm_and_wait(arm) == "timeout"  # no reader to ack
    # a reject arrives during the wait (wait for a NEW frame — the link already
    # holds frames from the calls above)
    plugin._ack_timeout_s = 1.0
    base = len(link.snapshot())

    def reject():
        for _ in range(1000):
            if len(link.snapshot()) > base:
                plugin._on_arduino_reject("d")
                return
            time.sleep(0.001)

    t = threading.Thread(target=reject)
    t.start()
    assert plugin._arm_and_wait(arm) == "reject"
    t.join(timeout=2.0)


def test_arm_timeout_reports_error_and_attempts_usb_reset(monkeypatch):
    import octacam.serial_ports as sp

    calls: list[str] = []
    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (calls.append(device), (False, "no"))[1])
    records, detach = _capture_octacam_logs()
    bc = _Broadcasts()
    try:
        plugin, link = _plugin_with_fake()
        plugin.set_broadcast(bc)
        plugin._ack_timeout_s = 0.03  # nothing acks
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    finally:
        detach()
    assert calls == [DEVICE]  # a USB-reset recovery was attempted
    assert link.opens >= 1 and link.closes >= 1  # link was cycled
    assert len(link.snapshot()) == 2  # armed, then re-armed after the reset
    assert any("did not arm" in r.getMessage() for r in records)
    assert bc.last_error() and "did not arm" in bc.last_error()


def test_arm_write_failure_is_reported_and_recovered(monkeypatch):
    import octacam.serial_ports as sp

    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (True, "reset"))
    plugin, link = _plugin_with_fake()
    bc = _Broadcasts()
    plugin.set_broadcast(bc)
    link.fail_writes = True  # wedged: every write fails
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    assert link.closes >= 1  # recovery cycled the link
    assert bc.last_error() is not None


def test_arm_recovery_success_rearms_and_clears_error(monkeypatch):
    import octacam.serial_ports as sp

    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (True, "reset"))
    plugin, link = _plugin_with_fake()
    bc = _Broadcasts()
    plugin.set_broadcast(bc)
    plugin._ack_timeout_s = 0.2  # first arm times out quickly, then recovery re-arms

    # Ack only the SECOND arm (after the USB-reset recovery), simulating a board
    # that comes back to life once its link is reset.
    def ack():
        for _ in range(2000):
            if len(link.snapshot()) >= 2:
                plugin._on_arduino_status("R")
                return
            time.sleep(0.001)

    t = threading.Thread(target=ack)
    t.start()
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    t.join(timeout=3.0)
    assert plugin._armed_event.is_set()
    assert plugin._last_error is None  # the successful re-arm cleared the error


def test_arm_reject_does_not_trigger_usb_reset(monkeypatch):
    import octacam.serial_ports as sp

    calls: list[str] = []
    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (calls.append(device), (True, "x"))[1])
    plugin, link = _plugin_with_fake()
    plugin._ack_timeout_s = 1.0

    def reject():
        for _ in range(1000):
            if link.snapshot():
                plugin._on_arduino_reject("r")  # reserved pin
                return
            time.sleep(0.001)

    t = threading.Thread(target=reject)
    t.start()
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    t.join(timeout=2.0)
    assert calls == []  # a protocol reject is not a wedge; no USB reset


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
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
        t.join(timeout=2.0)
    finally:
        detach()
    assert plugin._armed_event.is_set()
    assert plugin._last_error is None  # a clean ack reports no failure
    assert not any(r.levelno >= logging.ERROR for r in records)


def test_on_recording_start_logs_firmware_reject():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        plugin._ack_timeout_s = 1.0

        def reject():
            for _ in range(500):
                if link.snapshot():
                    plugin._on_arduino_reject("p")  # unknown pin id
                    return
                time.sleep(0.001)

        t = threading.Thread(target=reject)
        t.start()
        plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
        t.join(timeout=2.0)
    finally:
        detach()
    assert any("REJECTED" in r.getMessage() and "unknown pin" in r.getMessage() for r in records)


# ===========================================================================
# REST endpoints
# ===========================================================================


def _test_client(plugin: TriggerboxPlugin) -> TestClient:
    app = FastAPI()
    app.include_router(plugin.api_router())
    return TestClient(app)


def test_status_endpoint_reports_cameras_and_lights():
    plugin, _link = _plugin_with_fake()
    data = _test_client(plugin).get("/api/triggerbox/status").json()
    assert data["ready"] is True
    assert data["device"] == DEVICE
    assert [c["pin"] for c in data["cameras"]] == ["D13"]
    assert len(data["lights"]) == 2
    assert data["firmware_ok"] is True
    assert data["error"] is None  # no failure recorded yet


def test_status_endpoint_surfaces_last_error():
    plugin, _link = _plugin_with_fake()
    plugin._last_error = "the board did not arm"
    data = _test_client(plugin).get("/api/triggerbox/status").json()
    assert data["error"] == "the board did not arm"


def test_exposures_endpoint_lists_camera_timings():
    plugin, _link = _plugin_with_fake(strobe_guard_us=100)
    plugin.set_controller(FakeController([FakeCamera("a", 2000, 50)]))
    data = _test_client(plugin).get("/api/triggerbox/exposures").json()
    assert data["guard_us"] == 100
    assert data["cameras"] == [
        {"index": 0, "name": "a", "exposure_us": 2000.0, "trigger_delay_us": 50.0}
    ]


def test_exposures_endpoint_empty_without_controller():
    plugin, _link = _plugin_with_fake()
    assert _test_client(plugin).get("/api/triggerbox/exposures").json()["cameras"] == []


def test_reconnect_endpoint(monkeypatch):
    plugin, _link = _plugin_with_fake()
    monkeypatch.setattr(plugin, "_open", lambda: None)
    body = _test_client(plugin).post("/api/triggerbox/reconnect", json={}).json()
    assert body["ready"] is True and body["device"] == DEVICE


def test_reconnect_endpoint_surfaces_error(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: "boom")
    body = _test_client(plugin).post("/api/triggerbox/reconnect", json={}).json()
    assert body["ready"] is False and body["error"] == "boom"


def test_reconnect_endpoint_device_override(monkeypatch):
    plugin, _link = _plugin_with_fake()
    monkeypatch.setattr(plugin, "_open", lambda: None)
    _test_client(plugin).post("/api/triggerbox/reconnect", json={"device": "/dev/ttyUSB9"})
    assert plugin._configured_device == "/dev/ttyUSB9"


# ===========================================================================
# TriggerboxLink over a fake serial port
# ===========================================================================


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
    return SimpleNamespace(Serial=lambda *a, **k: fake, SerialException=_FakeSerialError)


def _wait(pred, timeout=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_link_send_arm_writes_exact_bytes(monkeypatch):
    import octacam.plugins.triggerbox as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    link = TriggerboxLink(on_status=lambda t: None)
    link.open(DEVICE, 115200)
    try:
        arm = ArmSpec(80, 5000, [(11, 500, 0)], [(3, 1, 0, 2500, 0, 0)])
        link.send_arm(arm)
        assert _wait(lambda: bytes(fake.written) == arm.to_bytes())
    finally:
        link.close()


def test_link_parses_status_and_reject_tokens(monkeypatch):
    import octacam.plugins.triggerbox as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    states: list[str] = []
    rejects: list[str] = []
    link = TriggerboxLink(on_status=states.append, on_reject=rejects.append)
    link.open(DEVICE, 115200)
    try:
        fake.feed(b"R\nTRIGGERBOX 2\nEp\nD\nC\n")
        assert _wait(lambda: states == ["R", "D", "C"])
        assert rejects == ["p"]
        assert link.identity == "TRIGGERBOX 2"
    finally:
        link.close()


def test_link_reassembles_split_tokens(monkeypatch):
    import octacam.plugins.triggerbox as m

    fake = _FakeSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    states: list[str] = []
    link = TriggerboxLink(on_status=states.append)
    link.open(DEVICE, 115200)
    try:
        fake.feed(b"R")
        fake.feed(b"\nD\n")
        assert _wait(lambda: states == ["R", "D"])
    finally:
        link.close()


def test_link_read_error_marks_broken(monkeypatch):
    import octacam.plugins.triggerbox as m

    fake = _FakeSerial()
    fake.raise_on_read = _FakeSerialError("unplugged")
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    broken = threading.Event()
    link = TriggerboxLink(on_status=lambda t: None, on_broken=broken.set)
    link.open(DEVICE, 115200)
    try:
        assert broken.wait(1.0)
        assert not link.is_open
    finally:
        link.close()


def test_link_is_open_false_when_never_opened():
    link = TriggerboxLink(on_status=lambda t: None)
    assert link.is_open is False


# ===========================================================================
# Firmware identity detection
# ===========================================================================


def test_verify_identity_accepts_triggerbox_v2():
    plugin, link = _plugin_with_fake()
    link.banner = "TRIGGERBOX 2"
    plugin._verify_identity()
    assert plugin._firmware == "TRIGGERBOX 2" and plugin._firmware_ok is True


def test_verify_identity_refuses_legacy_omniview():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        link.banner = "OMNIVIEW 1"
        plugin._verify_identity()
    finally:
        detach()
    assert plugin._firmware_ok is False
    assert any("reflash to TRIGGERBOX" in r.getMessage() for r in records)


def test_verify_identity_warns_on_version_mismatch():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        link.banner = "TRIGGERBOX 1"
        plugin._verify_identity()
    finally:
        detach()
    assert plugin._firmware_ok is True  # still a triggerbox board
    assert any("reflash" in r.getMessage() for r in records)


def test_verify_identity_proceeds_without_banner():
    plugin, link = _plugin_with_fake()
    link.banner = None
    plugin._verify_identity()
    assert plugin._firmware_ok is True


def test_open_attempts_usb_reset_when_board_is_silent(monkeypatch):
    import octacam.serial_ports as sp

    calls: list[str] = []
    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (calls.append(device), (False, "no"))[1])
    plugin, link = _plugin_with_fake()  # FakeLink.identify() returns None (silent)
    plugin._open()
    assert calls == [DEVICE]  # a wedge is suspected -> one bus reset attempted


def test_open_no_reset_for_healthy_board(monkeypatch):
    import octacam.serial_ports as sp

    calls: list[str] = []
    monkeypatch.setattr(sp, "reset_usb_device", lambda device: (calls.append(device), (True, "x"))[1])
    plugin, link = _plugin_with_fake()
    link.banner = "TRIGGERBOX 2"
    plugin._open()
    assert calls == []  # a healthy identify never triggers a reset
    assert plugin._firmware == "TRIGGERBOX 2"


# ===========================================================================
# Registry + alias
# ===========================================================================


def test_triggerbox_is_registered_builtin():
    assert "triggerbox" in _BUILTINS


def test_omniview_alias_resolves_to_triggerbox():
    assert _ALIASES.get("omniview") == "triggerbox"
    cfg = SimpleNamespace(plugins=[SimpleNamespace(name="omniview", options={"device": DEVICE})])
    manager = build_plugins(cfg, enabled=None)
    assert [p.name for p in manager.plugins] == ["triggerbox"]


def test_default_start_params_via_manager():
    cfg = SimpleNamespace(plugins=[SimpleNamespace(name="triggerbox", options={"device": DEVICE})])
    manager = build_plugins(cfg, enabled=None)
    params = manager.default_start_params(80.0, 5.0)
    assert "triggerbox" in params
    assert params["triggerbox"]["fps"] == 80
