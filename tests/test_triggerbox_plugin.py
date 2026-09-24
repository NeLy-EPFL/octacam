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

from octacam.plugins import _BUILTINS, build_plugins
from octacam.plugins.triggerbox import (
    _PROTOCOL_VERSION,
    PIN_LABELS,
    ArmSpec,
    LightChannel,
    TriggerboxLink,
    TriggerboxPlugin,
    _build,
    period_us,
    pin_id,
    plan_train,
    pulse_count,
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
# Preview arm (octacam-driven "managed" preview)
# ===========================================================================


def test_drives_preview_trigger_is_true():
    plugin, _link = _plugin_with_fake()
    assert plugin.drives_preview_trigger() is True


def test_preview_arm_matches_recording_except_indefinite_duration():
    plugin, link = _plugin_with_fake(
        cameras=[{"pin": "D13", "pulse_us": 500}],
        lights=[
            {"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": 20.0}
        ],
    )
    slice_ = plugin.default_start_params(80.0, 10.0)
    plugin.on_recording_start({"triggerbox": slice_})
    rec = _last_arm(link)
    plugin.on_preview_start({"triggerbox": slice_})
    prev = _last_arm(link)
    # Recording runs exactly its 800 pulses (ending once the last pulse and
    # strobe are out, see plan_train); preview runs until cancel (0)...
    assert rec["duration_ms"] == plan_train(80, 800, rec["cams"], rec["lights"]).duration_ms
    assert rec["duration_ms"] == 9991
    assert prev["duration_ms"] == 0
    # ...but is otherwise the same trigger + strobe as the recording (user chose
    # "strobe as in recording"), so preview is WYSIWYG.
    assert prev["fps"] == rec["fps"]
    assert prev["cams"] == rec["cams"]
    assert prev["lights"] == rec["lights"]


def test_on_preview_start_without_slice_uses_configured_spec():
    plugin, link = _plugin_with_fake(cameras=[{"pin": "D13", "pulse_us": 500}])
    plugin.on_preview_start(None)  # controller sent no slice
    arm = _last_arm(link)
    assert arm["duration_ms"] == 0
    assert arm["fps"] == plugin._default_fps


def test_on_preview_stop_cancels():
    plugin, link = _plugin_with_fake()
    plugin.on_preview_start({"triggerbox": plugin.default_start_params(80.0, 10.0)})
    plugin.on_preview_stop()
    assert link.snapshot()[-1] == bytes([CANCEL_MAGIC])


def _spec_msg(duty):
    return {
        "type": "triggerbox_spec",
        "spec": {
            "fps": 80,
            "cameras": [{"pin": "D13", "pulse_us": 500}],
            "lights": [
                {"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": duty}
            ],
        },
    }


def test_on_ws_message_ignores_unrelated():
    plugin, _link = _plugin_with_fake()
    assert plugin.on_ws_message({"type": "jog"}, 1) is False


def test_ws_spec_edit_updates_the_plugin_spec():
    # A live tab edit becomes the source the managed preview arm reads.
    plugin, _link = _plugin_with_fake(lights=[{"channel": 1, "mode": "off"}])
    assert plugin.on_ws_message(_spec_msg(20.0), 1) is True
    dsp = plugin.default_start_params(80.0, 10.0)
    assert dsp["lights"][0]["mode"] == "strobe"
    assert dsp["cameras"][0]["pin"] == "D13"


def test_ws_spec_edit_does_not_arm_when_not_previewing():
    plugin, link = _plugin_with_fake(lights=[{"channel": 1, "mode": "off"}])
    plugin.on_ws_message(_spec_msg(20.0), 1)
    assert [f for f in link.snapshot() if f and f[0] == ARM_MAGIC] == []


def test_ws_spec_edit_rearms_a_running_managed_preview():
    plugin, link = _plugin_with_fake(
        lights=[{"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": 20.0}]
    )
    plugin.on_preview_start(None)  # managed preview armed from the config spec
    before = len([f for f in link.snapshot() if f and f[0] == ARM_MAGIC])
    plugin.on_ws_message(_spec_msg(50.0), 1)  # operator drags duty to 50%
    arms = [f for f in link.snapshot() if f and f[0] == ARM_MAGIC]
    assert len(arms) == before + 1  # re-armed in place
    dec = _decode_frame(arms[-1])
    assert dec["duration_ms"] == 0  # still indefinite (preview)
    assert dec["lights"][0][3] == round(0.50 * (1_000_000 / 80))  # new 50% on-time


def test_ws_spec_edit_no_rearm_when_unchanged():
    plugin, link = _plugin_with_fake(
        lights=[{"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": 20.0}]
    )
    plugin.on_preview_start(None)
    plugin.on_ws_message(_spec_msg(20.0), 1)  # first push adopts the spec (may re-arm)
    n = len([f for f in link.snapshot() if f and f[0] == ARM_MAGIC])
    plugin.on_ws_message(_spec_msg(20.0), 1)  # identical spec (a redraw push) -> no-op
    assert len([f for f in link.snapshot() if f and f[0] == ARM_MAGIC]) == n


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
    # 2000 ms at 100 fps = 200 pulses; the run ends once the last one is out.
    assert dec["fps"] == 100
    assert dec["duration_ms"] == plan_train(100, 200, dec["cams"], dec["lights"]).duration_ms
    assert dec["duration_ms"] == 1992
    assert dec["cams"] == [(pin_id("D13"), 500, 0)]
    assert dec["lights"] == [(pin_id("D5"), 2, 0, 0, 0, 0)]


def test_on_recording_start_uses_configured_spec_when_only_fps_given():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    dec = _last_arm(link)
    assert len(dec["cams"]) == 1  # the classic D13 line
    assert len(dec["lights"]) == 2  # ch1 + ch2


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


def test_on_recording_start_reports_and_skips_when_link_closed():
    plugin, link = _plugin_with_fake(is_open=False)
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    assert not link.snapshot()  # nothing armed
    # Surfaced to the operator (not just logged), so a frozen preview/recording
    # has a visible cause.
    assert plugin._last_error and "not connected" in plugin._last_error


def test_on_recording_start_reports_and_skips_on_incompatible_firmware():
    plugin, link = _plugin_with_fake()
    plugin._firmware_ok = False
    plugin._firmware = "OTHERBOARD 1"
    plugin.on_recording_start({"triggerbox": {"fps": 80, "duration_ms": 1000}})
    assert not link.snapshot()
    assert plugin._last_error and "incompatible" in plugin._last_error


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


def _tab_spec(plugin: TriggerboxPlugin, **light_changes) -> dict:
    """A start slice shaped like the tab's getStartParams(): full-key camera and
    light dicts seeded from status(), off channels left out, per-channel edits
    applied from ``light_changes`` ({"ch1": {...}})."""
    status = plugin.status()
    by_channel = {lt["channel"]: dict(lt) for lt in status["lights"]}
    for key, change in light_changes.items():
        channel = int(key.removeprefix("ch"))
        base = by_channel.get(channel, LightChannel(channel=channel).to_dict())
        by_channel[channel] = {**base, **change}
    lights = [by_channel[ch] for ch in sorted(by_channel) if by_channel[ch]["mode"] != "off"]
    return {"fps": 80, "duration_ms": 5000, "cameras": status["cameras"], "lights": lights}


_SNAPSHOT_RIG = {
    "cameras": [{"pin": "D13", "pulse_us": 500}],
    "lights": [
        {"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": 25.0},
        {"channel": 2, "mode": "strobe", "duty_mode": "manual", "duty_percent": 25.0},
        {"channel": 3, "mode": "off"},
    ],
}


def test_snapshot_options_none_when_the_config_already_matches():
    plugin = _build(_SNAPSHOT_RIG)
    # Not armed with the recording: nothing to record.
    assert plugin.snapshot_options(None) is None
    assert plugin.snapshot_options({"twophoton": {}}) is None
    # An untouched tab (which never sends the off channel 3) and the headless
    # CLI slice (which does) both match the config, so the snapshot stays verbatim.
    assert plugin.snapshot_options({"triggerbox": _tab_spec(plugin)}) is None
    headless = plugin.default_start_params(80.0, 5.0)
    assert plugin.snapshot_options({"triggerbox": headless}) is None


def test_snapshot_options_carry_the_armed_lights_and_cameras():
    plugin = _build(_SNAPSHOT_RIG)
    spec = _tab_spec(
        plugin,
        ch1={"duty_percent": 60.0},
        ch2={"mode": "off"},
        ch3={"mode": "continuous"},
    )
    spec["cameras"] = [{"pin": "D13", "pulse_us": 700, "delay_us": 0}]
    # The tab pushes each edit to the plugin as it happens, well before the
    # recording starts; that must not hide the edit from the snapshot.
    assert plugin.on_ws_message({"type": "triggerbox_spec", "spec": spec}, 1)
    options = plugin.snapshot_options({"triggerbox": spec})
    assert options is not None
    assert options["cameras"] == [{"pin": "D13", "pulse_us": 700, "delay_us": 0}]
    assert [(lt["channel"], lt["mode"]) for lt in options["lights"]] == [
        (1, "strobe"),
        (3, "continuous"),
    ]
    # Reloading those options arms the board exactly as this recording did.
    relaunched = _build({**_SNAPSHOT_RIG, **options})
    assert relaunched._cameras == plugin._cameras_from_spec(spec)
    assert relaunched._lights == plugin._lights_from_spec(spec)


def test_snapshot_options_none_after_edits_are_reverted():
    plugin = _build(_SNAPSHOT_RIG)
    edited = _tab_spec(plugin, ch1={"duty_percent": 60.0})
    original = _tab_spec(plugin)
    plugin.on_ws_message({"type": "triggerbox_spec", "spec": edited}, 1)
    plugin.on_ws_message({"type": "triggerbox_spec", "spec": original}, 1)
    assert plugin.snapshot_options({"triggerbox": original}) is None


def test_snapshot_options_all_lights_off_reloads_as_off():
    plugin = _build(_SNAPSHOT_RIG)
    spec = _tab_spec(plugin, ch1={"mode": "off"}, ch2={"mode": "off"})
    options = plugin.snapshot_options({"triggerbox": spec})
    assert options is not None and options["lights"] == []
    # An explicit empty list means "all off", unlike an absent key (which
    # defaults to the classic two strobes).
    assert _build({"lights": options["lights"]})._lights == []


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


def test_recover_usb_holds_port_lock(monkeypatch):
    # Fix 1: _recover_usb must run under the provisioner's port_lock so a
    # concurrent flash (which holds the same lock across close->upload->reopen)
    # can't fight over the tty. With another thread holding the (re-entrant) lock,
    # a _recover_usb on a different thread must block before it resets the device.
    import octacam.serial_ports as sp

    reset_calls: list[str] = []
    monkeypatch.setattr(
        sp, "reset_usb_device",
        lambda device: (reset_calls.append(device), (True, "reset"))[1],
    )
    plugin, link = _plugin_with_fake()
    started = threading.Event()
    done = threading.Event()

    def worker():
        started.set()
        plugin._recover_usb("test wedge")
        done.set()

    with plugin._fw.port_lock:
        t = threading.Thread(target=worker)
        t.start()
        assert started.wait(1.0)
        time.sleep(0.1)  # give the worker a chance to (fail to) proceed
        assert reset_calls == []  # blocked on port_lock; no reset yet
        assert not done.is_set()
    t.join(timeout=2.0)
    assert done.is_set()
    assert reset_calls == [DEVICE]  # proceeded once the lock was released


def test_arm_and_wait_serialized_by_arm_lock():
    # Fix 6: the clear+send+wait window must be serialized so two concurrent arms
    # can't share _armed_event/_last_reject and steal each other's ack/reject.
    plugin, link = _plugin_with_fake()
    plugin._ack_timeout_s = 0.0
    gate = threading.Event()
    in_send = threading.Event()

    def blocking_send(spec):
        in_send.set()
        gate.wait(2.0)
        return True

    link.send_arm = blocking_send
    arm = plugin._build_arm_spec(80, 1000, plugin._cameras, plugin._lights)
    t = threading.Thread(target=lambda: plugin._arm_and_wait(arm))
    t.start()
    try:
        assert in_send.wait(1.0)  # arm A is inside the locked send window
        # A holds _arm_lock across the whole send+wait, so a concurrent acquire fails.
        assert plugin._arm_lock.acquire(blocking=False) is False
    finally:
        gate.set()
    t.join(timeout=2.0)
    # Once A finished the lock is free again.
    assert plugin._arm_lock.acquire(blocking=False) is True
    plugin._arm_lock.release()


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


def test_exposures_endpoint_follows_a_late_attached_camera_system():
    """The endpoint is a live read, not a snapshot taken at set_controller time.

    Serve-first startup hands the plugin a controller whose camera system is
    still the hardware-free placeholder (zero cameras), and swaps in the real one
    seconds later. The same client must see the exposures appear — that is what
    makes the tab's re-read on the init push (triggerbox.js applyStatus) work."""
    plugin, _link = _plugin_with_fake(strobe_guard_us=100)
    controller = FakeController([])
    plugin.set_controller(controller)
    client = _test_client(plugin)

    assert client.get("/api/triggerbox/exposures").json()["cameras"] == []
    assert plugin._auto_led_on_us() is None

    controller.camera_system = [FakeCamera("a", 5000, 0)]

    assert client.get("/api/triggerbox/exposures").json()["cameras"] == [
        {"index": 0, "name": "a", "exposure_us": 5000.0, "trigger_delay_us": 0.0}
    ]
    assert plugin._auto_led_on_us() == 5100.0


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


def test_verify_identity_refuses_foreign_board():
    records, detach = _capture_octacam_logs()
    try:
        plugin, link = _plugin_with_fake()
        link.banner = "OTHERBOARD 1"
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
    # A v1 board can't parse a v2 arm packet, so arming is disabled and a reflash
    # is offered (better than arming and getting a guaranteed protocol reject).
    assert plugin._firmware_ok is False
    assert plugin.firmware_provisioning()["state"] == "wrong_version"
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


def test_default_start_params_via_manager():
    cfg = SimpleNamespace(plugins=[SimpleNamespace(name="triggerbox", options={"device": DEVICE})])
    manager = build_plugins(cfg, enabled=None)
    params = manager.default_start_params(80.0, 5.0)
    assert "triggerbox" in params
    assert params["triggerbox"]["fps"] == 80


# ===========================================================================
# Firmware provisioning: identity classification, flash, endpoints
# ===========================================================================

import octacam.firmware as fw_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_flash(monkeypatch):
    """Never shell out to arduino-cli from the suite, and make can_flash
    deterministic regardless of whether the dev box has arduino-cli installed."""
    monkeypatch.setattr(fw_mod, "arduino_cli_path", lambda: "/fake/arduino-cli")

    def _fake_flash(spec, port, needed_build, **kwargs):
        return fw_mod.FlashResult(
            True, f"uploaded build {needed_build} to {port}", "compiled\nuploaded",
            build=needed_build,
        )

    monkeypatch.setattr(fw_mod, "flash", _fake_flash)


def _verify_with_banner(plugin: TriggerboxPlugin, link: FakeLink, banner):
    """Set the board's identity banner and run the plugin's classification."""
    link.banner = banner
    plugin._verify_identity()


def test_identify_current_build_is_up_to_date():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, f"TRIGGERBOX 2 {plugin._fw_needed_build}")
    assert plugin._firmware_ok
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "current"
    assert prov["needs_flash"] is False


def test_identify_outdated_build_still_arms():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")  # no build tag = pre-fingerprint flash
    assert plugin._firmware_ok  # OUTDATED is still wire-compatible; arming stays enabled
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "outdated"
    assert prov["needs_flash"] is True


def test_identify_wrong_version_disables_arming():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 1 abc1234")
    assert plugin._firmware_ok is False
    assert plugin.firmware_provisioning()["state"] == "wrong_version"


def test_identify_foreign_board_disables_arming_and_needs_confirm():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "OTHERBOARD 1")
    assert plugin._firmware_ok is False
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "wrong_board"
    # A foreign board is flashable but never auto-flashed without confirmation.
    assert prov["needs_flash"] and not prov["safe_to_auto_flash"]


def test_identify_unidentified_proceeds_but_flags_flash():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, None)
    assert plugin._firmware_ok  # unknown -> proceed (may be a slow link)
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "unidentified"
    assert prov["needs_flash"] and not prov["safe_to_auto_flash"]


def test_no_source_falls_back_to_banner_compatibility():
    plugin, link = _plugin_with_fake()
    plugin._fw_spec = None
    plugin._fw_needed_build = None
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")
    assert plugin._firmware_ok  # name+version compatible; no flash offer
    assert plugin._fw_check is None
    _verify_with_banner(plugin, link, "TRIGGERBOX 1")
    assert plugin._firmware_ok is False


def test_broadcast_includes_firmware_state():
    plugin, link = _plugin_with_fake()
    bc = _Broadcasts()
    plugin.set_broadcast(bc)
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")
    plugin._broadcast_state()
    last = bc.msgs[-1]
    assert last["firmware_state"] == "outdated"
    assert last["needs_flash"] is True


def test_flash_firmware_success_updates_state():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")  # start out of date
    # After the (faked) upload the board reports the current build on re-identify.
    link.banner = f"TRIGGERBOX 2 {plugin._fw_needed_build}"
    result = plugin.flash_firmware()
    assert result.ok
    assert plugin._firmware_ok
    assert plugin.firmware_provisioning()["needs_flash"] is False
    assert link.closes >= 1 and link.opens >= 1  # link cycled for the upload


def test_flash_firmware_refused_while_running():
    plugin, link = _plugin_with_fake()
    plugin._arduino_state = "running"
    result = plugin.flash_firmware()
    assert not result.ok
    assert "running" in result.message


def test_flash_firmware_without_source():
    plugin, link = _plugin_with_fake()
    plugin._fw_spec = None
    plugin._fw_needed_build = None
    result = plugin.flash_firmware()
    assert not result.ok
    assert "source" in result.message.lower()


def test_flash_firmware_failure_is_reported(monkeypatch):
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")
    monkeypatch.setattr(
        fw_mod, "flash",
        lambda spec, port, build, **k: fw_mod.FlashResult(False, "arduino-cli exited 1", "boom"),
    )
    result = plugin.flash_firmware()
    assert not result.ok
    assert plugin._last_error == result.message  # surfaced to the GUI


def test_firmware_endpoint():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")
    body = _test_client(plugin).get("/api/triggerbox/firmware").json()
    assert body["state"] == "outdated"
    assert body["needs_flash"] is True
    assert body["can_flash"] is True
    assert body["needed_build"] == plugin._fw_needed_build


def test_flash_endpoint():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2")
    link.banner = f"TRIGGERBOX 2 {plugin._fw_needed_build}"
    body = _test_client(plugin).post("/api/triggerbox/flash", json={}).json()
    assert body["ok"] is True
    assert body["provisioning"]["needs_flash"] is False
    assert "log" in body


def test_build_reads_auto_flash_option():
    assert _build({"device": DEVICE}).firmware_provisioning()["auto_flash"] is False
    plugin = _build({"device": DEVICE, "auto_flash": True})
    assert plugin._auto_flash is True
    assert plugin.firmware_provisioning()["auto_flash"] is True


# ===========================================================================
# Exact trains, priming, acknowledged cancel, prompt tokens
# ===========================================================================


def test_trigger_train_describes_the_exact_train():
    plugin, _link = _plugin_with_fake()
    assert plugin.trigger_train({"triggerbox": {"fps": 125, "duration_ms": 900_000}}) == {
        "fps": 125,
        "period_ns": 8_000_000,
        "count": 112_500,
    }
    # 90 fps: the board's integer period, and round(fps * duration) pulses.
    train = plugin.trigger_train({"triggerbox": {"fps": 90, "duration_ms": 10_000}})
    assert train == {"fps": 90, "period_ns": 11_111_000, "count": 900}
    assert plugin.trigger_train(None) is None


# --- A model of how the board ends a finite run (triggerbox.ino), kept apart
# from the plugin's own: camera line i rises at k * period + delay_i for frame
# k; the run goes idle (every output LOW) once millis() - start >= duration_ms,
# i.e. anywhere in the duration's last millisecond, give or take the loop's
# polling; an edge due in the pass that sees the end still goes out.


def _fw_line(period, pulse_us, delay_us):
    """A camera line as prepare_outputs() clamps it: (delay, pulse)."""
    delay = min(delay_us, period - 1)
    pulse = max(pulse_us or 500, 5)
    return delay, min(pulse, period - 1 - delay if period > delay + 1 else 1)


def _fw_emitted(period, line, end_us):
    """(pulses a line emits, µs its last one is high) for an idle at ``end_us``
    from the train's first frame edge."""
    delay, pulse = line
    if end_us < delay:
        return 0, 0
    n = (end_us - delay) // period + 1
    return n, min(pulse, end_us - ((n - 1) * period + delay))


def _fw_ends(duration_ms):
    """The earliest and latest idle of a run of ``duration_ms``."""
    from octacam.plugins.triggerbox import RUN_END_EARLY_US, RUN_END_LATE_US

    return duration_ms * 1000 - 1000 - RUN_END_EARLY_US, duration_ms * 1000 + RUN_END_LATE_US


def _fw_strobe_done(period, light, count, duration_ms):
    """Whether a strobe record's on-time of the train's last frame is over when
    a run of ``duration_ms`` ends (a continuous light or pulse train has none).
    A strobe may lose the few µs by which the run can end early, so this takes
    the nominal earliest end, a whole millisecond before the duration."""
    _pin, mode, delay, on_us, _p2, _p3 = light
    if mode != 1 or not 0 < on_us < period:
        return True
    on = min(delay, period - 1)
    return (count - 1) * period + min(on + on_us, period) <= duration_ms * 1000 - 1000


def _fw_exact(fps, cams, duration_ms, count):
    """Every line emits exactly ``count`` complete pulses for every end."""
    period = period_us(fps)
    first, last = _fw_ends(duration_ms)
    for _pin, pulse_us, delay_us in cams:
        line = _fw_line(period, pulse_us, delay_us)
        if _fw_emitted(period, line, first) != (count, line[1]):
            return False
        if _fw_emitted(period, line, last)[0] != count:
            return False
    return True


def _train_count(plugin, params):
    """The pulse count trigger_train tells the recording."""
    train = plugin.trigger_train(params)
    assert train is not None
    return train["count"]


_DURATIONS_MS = (20, 1000, 7_300, 10_000, 12_345, 600_000, 3_600_000)
_D13 = [(pin_id("D13"), 500, 0)]


def test_plan_train_counts_what_the_board_emits_at_every_gui_fps():
    # The GUI allows 1-1000 fps. Wherever a millisecond end can separate the
    # last pulse from the next frame edge the plan is exact (every line gets
    # exactly `count` complete pulses, whatever the run clock's phase); below
    # 400 fps that is always the requested count.
    from octacam.plugins.triggerbox import MAX_COUNT_SHIFT

    for fps in range(1, 1001):
        period = period_us(fps)
        line = _fw_line(period, 500, 0)
        for duration_ms in _DURATIONS_MS:
            wanted = pulse_count(fps, duration_ms)
            plan = plan_train(fps, wanted, _D13)
            where = (fps, duration_ms, plan)
            assert abs(plan.count - wanted) <= MAX_COUNT_SHIFT, where
            first, last = _fw_ends(plan.duration_ms)
            if fps < 400:
                assert plan.exact and plan.count == wanted, where
            if plan.exact:
                assert _fw_exact(fps, _D13, plan.duration_ms, plan.count), where
            elif plan.certain:
                # The last pulse may be cut short, never below half its width.
                n_first, high = _fw_emitted(period, line, first)
                assert n_first == _fw_emitted(period, line, last)[0] == plan.count, where
                assert 2 * high >= line[1], where
            else:
                # Not guaranteed: but the likeliest outcome over the end window.
                outcomes = [
                    _fw_emitted(period, line, end)[0] for end in range(first, last + 1, 5)
                ]
                assert max(set(outcomes), key=outcomes.count) == plan.count, where


@pytest.mark.parametrize("fps", [90, 300, 333, *range(400, 505), *range(505, 1001, 7), 1000])
def test_plan_train_misses_no_exact_count(fps):
    # Brute force over counts and durations with the board model: an exact plan
    # exists within the shift iff the planner finds one, at the smallest shift.
    from octacam.plugins.triggerbox import MAX_COUNT_SHIFT

    period = period_us(fps)
    for duration_ms in (1000, 10_000, 12_345):
        wanted = pulse_count(fps, duration_ms)
        plan = plan_train(fps, wanted, _D13)
        shifts = [
            abs(count - wanted)
            for count in range(wanted - MAX_COUNT_SHIFT, wanted + MAX_COUNT_SHIFT + 1)
            if count >= 1
            and any(
                _fw_exact(fps, _D13, d, count)
                for d in range((count - 1) * period // 1000, count * period // 1000 + 3)
            )
        ]
        assert plan.exact == bool(shifts), (fps, duration_ms, plan)
        if shifts:
            assert abs(plan.count - wanted) == min(shifts), (fps, duration_ms, plan)


def test_plan_train_ends_after_the_last_strobe_and_delayed_pulses():
    # Several camera lines (one delayed) and strobes: exact, and the end comes
    # after every line's last pulse and every strobe's last on-time, before the
    # next frame edge — for every fps and duration where that fits.
    cams = [(pin_id("D13"), 500, 0), (pin_id("D12"), 300, 2000)]
    for fps in (1, 10, 50, 80, 90, 100, 125, 150, 200):
        period = period_us(fps)
        lights = [
            (pin_id("D5"), 1, 0, period * 3 // 10, 0, 0),  # 30 % duty
            (pin_id("D6"), 1, 1000, 1500, 0, 0),  # delayed strobe
            (pin_id("D7"), 2, 0, 0, 0, 0),  # continuous: nothing to finish
        ]
        for duration_ms in _DURATIONS_MS:
            wanted = pulse_count(fps, duration_ms)
            plan = plan_train(fps, wanted, cams, lights)
            where = (fps, duration_ms, plan)
            assert plan.exact and plan.count == wanted, where
            assert plan.lights_cut == () and not plan.light_overrun, where
            assert _fw_exact(fps, cams, plan.duration_ms, plan.count), where
            done = [_fw_strobe_done(period, lt, plan.count, plan.duration_ms) for lt in lights]
            assert all(done), where
            assert _fw_ends(plan.duration_ms)[1] < plan.count * period, where  # no next edge


def test_plan_train_count_does_not_depend_on_the_lights():
    # trigger_train plans without light timing (no camera reads under the
    # controller lock); the arm plans with it. They must agree on the count.
    for fps in (80, 125, 333, 450, 505, 600, 777, 1000):
        period = period_us(fps)
        wanted = pulse_count(fps, 10_000)
        bare = plan_train(fps, wanted, _D13)
        for on_us in (0, 1, period // 5, period // 2, period - 1, period, 10 * period):
            for delay in (0, period // 3, period - 1):
                lights = [(pin_id("D5"), 1, delay, on_us, 0, 0)]
                assert plan_train(fps, wanted, _D13, lights).count == bare.count


def test_plan_train_ends_just_before_the_next_edge_when_a_strobe_cannot_finish():
    # A 95 % strobe cannot finish a millisecond before the next frame edge: the
    # count is still exact, the run ends as late as it can, and the plan says by
    # how much the last strobe may be cut.
    period = period_us(80)
    light = (pin_id("D5"), 1, 0, period * 95 // 100, 0, 0)
    plan = plan_train(80, 800, _D13, [light])
    assert plan.exact and plan.count == 800
    first, last = _fw_ends(plan.duration_ms)
    assert last < 800 * period and last + 1000 >= 800 * period  # as late as it can
    ((index, cut_us),) = plan.lights_cut
    assert index == 0
    assert cut_us == 799 * period + period * 95 // 100 - (plan.duration_ms * 1000 - 1000)


def test_trigger_train_counts_the_pulses_the_arm_emits():
    # Through the plugin: the count the recording is told is the count the arm's
    # duration makes the board emit — also above 500 fps, where a run ended
    # half a period after the last pulse emitted up to 9 fewer.
    for fps in (90, 300, 333, 401, 450, 480, 504, 505, 550, 610):
        plugin, link = _plugin_with_fake(cameras=[{"pin": "D13", "pulse_us": 500}])
        params = {"triggerbox": {"fps": fps, "duration_ms": 10_000}}
        count = _train_count(plugin, params)
        plugin.on_recording_start(params)
        arm = _last_arm(link)
        assert _fw_exact(fps, arm["cams"], arm["duration_ms"], count), (fps, count, arm)


def test_a_delayed_camera_line_gets_its_last_pulse():
    # A line delayed past half the period used to lose its last pulse: the run
    # ended half a period after the last frame edge.
    for delay_us in (5500, 6000, 7000, 9000):
        plugin, link = _plugin_with_fake(
            cameras=[{"pin": "D13", "pulse_us": 500, "delay_us": delay_us}]
        )
        params = {"triggerbox": {"fps": 100, "duration_ms": 10_000}}
        count = _train_count(plugin, params)
        plugin.on_recording_start(params)
        arm = _last_arm(link)
        assert count == 1000
        assert _fw_exact(100, arm["cams"], arm["duration_ms"], count), (delay_us, arm)


@pytest.mark.parametrize(
    ("fps", "light", "exposure_us"),
    [
        (80, {"mode": "strobe", "duty_mode": "manual", "duty_percent": 60}, None),
        (125, {"mode": "strobe", "duty_mode": "auto"}, 5000),
        (100, {"mode": "strobe", "duty_mode": "manual", "duty_percent": 40, "delay_us": 4000}, None),
    ],
)
def test_the_last_strobe_finishes_before_the_run_ends(fps, light, exposure_us):
    # The run used to end half a period after the last pulse, cutting a strobe
    # longer than that (60 % at 80 fps; a 5 ms exposure's auto duty at 125 fps).
    plugin, link = _plugin_with_fake(
        cameras=[{"pin": "D13", "pulse_us": 500}],
        lights=[{"channel": 1, **light}],
        strobe_guard_us=100,
    )
    if exposure_us is not None:
        controller = FakeController([FakeCamera("a", exposure_us, 0)])
        plugin.set_controller(controller)  # pyright: ignore[reportArgumentType]
    params = {"triggerbox": {"fps": fps, "duration_ms": 10_000}}
    count = _train_count(plugin, params)
    plugin.on_recording_start(params)
    arm = _last_arm(link)
    assert _fw_exact(fps, arm["cams"], arm["duration_ms"], count)
    assert _fw_strobe_done(period_us(fps), arm["lights"][0], count, arm["duration_ms"]), arm


def test_arm_warns_when_the_train_cannot_end_cleanly():
    logger = logging.getLogger("octacam")
    level = logger.level
    logger.setLevel(logging.INFO)
    records, detach = _capture_octacam_logs()
    try:
        # 1000 fps: no millisecond end separates the last pulse from the next
        # frame edge; 450 fps: one does, a pulse later; 95 % duty: the last strobe
        # cannot finish first.
        for fps, duty in ((1000, 20), (450, 20), (80, 95)):
            plugin, _link = _plugin_with_fake(
                cameras=[{"pin": "D13", "pulse_us": 500}],
                lights=[{"channel": 1, "mode": "strobe", "duty_percent": duty}],
            )
            plugin.on_recording_start({"triggerbox": {"fps": fps, "duration_ms": 10_000}})
    finally:
        detach()
        logger.setLevel(level)
    messages = [(r.levelno, r.getMessage()) for r in records]
    assert any(
        level == logging.WARNING and "at 1000 fps" in m and "cannot end the train" in m
        for level, m in messages
    ), messages
    assert any(
        level == logging.INFO and "at 450 fps" in m and "arming 4501 pulses" in m
        for level, m in messages
    ), messages
    assert any(
        level == logging.WARNING and "last strobe on D5" in m for level, m in messages
    ), messages


class _AckingLink(FakeLink):
    """A FakeLink whose board answers: 'R' on an arm, 'D' once a finite run is
    over, 'C' on a cancel — delivered to the plugin like the reader thread."""

    def __init__(self, plugin_ref, *, ack_cancel=True):
        super().__init__()
        self.plugin_ref = plugin_ref
        self.ack_cancel = ack_cancel

    def _later(self, delay, token):
        threading.Timer(delay, lambda: self.plugin_ref[0]._on_arduino_status(token)).start()

    def send_arm(self, spec):
        ok = super().send_arm(spec)
        if ok:
            self._later(0.005, "R")
            if spec.duration_ms:
                self._later(0.005 + spec.duration_ms / 1000, "D")
        return ok

    def send_cancel(self):
        super().send_cancel()
        if self.ack_cancel:
            self._later(0.005, "C")


def _acking_plugin(**kwargs):
    plugin = _build({"device": DEVICE, **kwargs})
    ref = [plugin]
    link = _AckingLink(ref, **{k: v for k, v in kwargs.items() if k == "ack_cancel"})
    plugin._link = link
    plugin._ack_timeout_s = 0.5
    return plugin, link


def test_prime_trigger_sends_camera_lines_only_and_waits_for_the_burst():
    plugin, link = _acking_plugin(
        cameras=[{"pin": "D13", "pulse_us": 500}],
        lights=[{"channel": 1, "mode": "strobe", "duty_mode": "manual", "duty_percent": 25}],
    )
    spec = plugin.default_start_params(125.0, 10.0)
    started = time.monotonic()
    assert plugin.prime_trigger({"triggerbox": spec}, 4) is True
    elapsed = time.monotonic() - started
    arm = _last_arm(link)
    assert arm["lights"] == []  # no light flash before the recording
    assert arm["cams"] == [(pin_id("D13"), 500, 0)]
    assert arm["duration_ms"] == plan_train(125, 4, arm["cams"]).duration_ms
    # it returned on the board's 'D', i.e. after the burst was out
    assert elapsed >= arm["duration_ms"] / 1000


def test_prime_trigger_declines_without_a_camera_line_or_a_board():
    plugin, _link = _plugin_with_fake(is_open=False)
    assert plugin.prime_trigger({"triggerbox": plugin.default_start_params(80, 1)}, 4) is False
    plugin, _link = _plugin_with_fake()
    assert plugin.prime_trigger(None, 4) is False


def test_on_preview_stop_waits_for_the_boards_cancel_ack():
    plugin, link = _acking_plugin()
    plugin._idle_event.clear()
    plugin.on_preview_stop()
    assert plugin._idle_event.is_set()  # returned only once 'C' arrived
    assert link.snapshot()[-1] == bytes([CANCEL_MAGIC])


def test_an_unacknowledged_cancel_is_bounded(caplog):
    plugin, _link = _acking_plugin(ack_cancel=False)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="octacam"):
        plugin.on_preview_stop()
    assert time.monotonic() - started < 1.0


class _BlockingSerial(_FakeSerial):
    """pyserial's read(n) semantics: block until n bytes or the port timeout."""

    timeout = 0.2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buf = bytearray()
        self._cv = threading.Condition()

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._buf.extend(data)
            self._cv.notify_all()

    @property
    def in_waiting(self) -> int:
        return len(self._buf)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        with self._cv:
            while len(self._buf) < n and self.is_open:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out


def test_status_tokens_arrive_without_waiting_for_the_port_timeout(monkeypatch):
    import octacam.plugins.triggerbox as m

    fake = _BlockingSerial()
    monkeypatch.setattr(m, "serial", _fake_serial_ns(fake))
    seen = []
    link = TriggerboxLink(on_status=lambda t: seen.append((time.monotonic(), t)))
    link.open(DEVICE, 115200)
    try:
        time.sleep(0.05)
        sent = time.monotonic()
        fake.feed(b"R\n")
        assert _wait(lambda: seen, timeout=1.0)
        # A read(64) would have held the 2-byte token for the full 0.2 s timeout.
        assert seen[0][1] == "R" and seen[0][0] - sent < 0.1
    finally:
        link.close()
