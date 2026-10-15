"""2-photon trigger plugin: wire format, hooks, REST endpoints, broadcast."""

import json
import queue
import struct
import threading
from types import SimpleNamespace
from typing import Any, cast

import serial
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import wait_until

from octacam.plugins import twophoton
from octacam.plugins.base import PluginManager
from octacam.plugins.twophoton import (
    DEFAULT_DURATION_MS,
    DEFAULT_FPS,
    DEFAULT_STOP_IDLE_MS,
    MIN_STOP_IDLE_MS,
    ArmParams,
    TwoPhotonLink,
    TwoPhotonPlugin,
    parse_stop_report,
)

# Wire-format constants (mirror the firmware and plugin source)
ARM_MAGIC = 0xA5
CANCEL_MAGIC = 0xCA
# magic(u8) + fps(u16) + duration_ms(u32) + idle_ms(u16) = 9 bytes
ARM_FORMAT = "<BHIH"


# ---------------------------------------------------------------------------
# FakeLink -- stands in for TwoPhotonLink without any serial port
# ---------------------------------------------------------------------------


class FakeLink(TwoPhotonLink):
    """The real link over no port: it records what is written, answers an arm
    with 'A' as the board does (unless `acks` is off), and identify with
    `banner`.
    """

    def __init__(self, plugin, is_open: bool = True):
        super().__init__(
            plugin._on_status, plugin._on_link_broken, plugin._on_stop_report
        )
        self._open = is_open
        self._lock = threading.Lock()
        self.written: list[bytes] = []
        self.acks = True
        self.fail_writes = False
        self.banner: str | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self, device, baud) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def identify(self, timeout: float = 0.5):
        self.identity = self.banner
        return self.banner

    def _write(self, data: bytes) -> bool:
        with self._lock:
            if not self._open or self.fail_writes:
                return False
            self.written.append(data)
        if data[0] == ARM_MAGIC and self.acks:
            self._feed(b"A")
        return True

    def snapshot(self) -> list[bytes]:
        with self._lock:
            return list(self.written)


def _plugin_with_fake(
    is_open: bool = True, match_thorimage: bool = False
) -> tuple[TwoPhotonPlugin, FakeLink]:
    """Return a TwoPhotonPlugin whose link is replaced with a FakeLink."""
    plugin = TwoPhotonPlugin(
        device="/dev/arduinoCams",
        default_fps=DEFAULT_FPS,
        default_duration_ms=DEFAULT_DURATION_MS,
        match_thorimage=match_thorimage,
    )
    link = FakeLink(plugin, is_open=is_open)
    plugin._link = link
    return plugin, link


# ---------------------------------------------------------------------------
# ArmParams: wire format
# ---------------------------------------------------------------------------


def test_arm_params_to_bytes_correct_format():
    params = ArmParams(fps=100, duration_ms=10_000)
    raw = params.to_bytes()
    assert len(raw) == 9
    magic, fps, duration_ms, idle_ms = struct.unpack(ARM_FORMAT, raw)
    assert magic == ARM_MAGIC
    assert fps == 100
    assert duration_ms == 10_000
    assert idle_ms == 0  # a fixed duration


def test_arm_params_to_bytes_boundary_values():
    params = ArmParams(fps=1, duration_ms=1)
    magic, fps, dur, _idle = struct.unpack(ARM_FORMAT, params.to_bytes())
    assert magic == ARM_MAGIC
    assert fps == 1
    assert dur == 1

    params = ArmParams(fps=10_000, duration_ms=2**32 - 1)
    magic, fps, dur, _idle = struct.unpack(ARM_FORMAT, params.to_bytes())
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
    p_low = ArmParams.from_payload({"fps": 0}, 100, 1000)
    p_high = ArmParams.from_payload({"fps": 99_999}, 100, 1000)
    assert p_low.fps == 1  # clamped to min
    assert p_high.fps == 10_000  # clamped to max


def test_arm_params_from_payload_clamps_duration():
    p = ArmParams.from_payload({"duration_ms": -5}, 100, 1000)
    assert p.duration_ms == 1  # clamped to min


def test_arm_params_from_payload_handles_invalid_types():
    params = ArmParams.from_payload({"fps": "bad", "duration_ms": None}, 80, 8000)
    assert params.fps == 80
    assert params.duration_ms == 8000


# ---------------------------------------------------------------------------
# Plugin: recording lifecycle hooks
# ---------------------------------------------------------------------------


def test_on_recording_start_sends_arm_packet():
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start({"fps": 120, "duration_ms": 5000})
    written = link.snapshot()
    assert len(written) == 1
    magic, fps, dur, _idle = struct.unpack(ARM_FORMAT, written[0])
    assert magic == ARM_MAGIC
    assert fps == 120
    assert dur == 5000


def test_on_recording_start_does_not_arm_when_no_params():
    # params=None means "arm with recording" was unchecked -- must not send anything.
    plugin, link = _plugin_with_fake()
    plugin.on_recording_start(None)
    assert link.snapshot() == []


def test_on_recording_start_does_not_arm_when_plugin_key_absent():
    # A different plugin's params present but no "twophoton" key -> do not arm.
    plugin, link = _plugin_with_fake()
    PluginManager([plugin]).on_recording_start({"flywheel": {"n_steps": 100}})
    assert link.snapshot() == []


def test_on_recording_start_uses_defaults_when_twophoton_key_present_but_empty():
    # {"twophoton": {}} means checkbox was checked, GUI omitted optional fields.
    plugin, link = _plugin_with_fake()
    PluginManager([plugin]).on_recording_start({"twophoton": {}})
    written = link.snapshot()
    assert len(written) == 1
    _, fps, dur, _idle = struct.unpack(ARM_FORMAT, written[0])
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
    plugin.board_state = "triggered"
    plugin.on_recording_stop(aborted=False)
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]
    assert plugin.board_state == "idle"


def test_on_recording_start_silently_skips_when_port_closed():
    plugin, link = _plugin_with_fake(is_open=False)
    plugin.on_recording_start(None)  # must not raise
    assert link.snapshot() == []


def test_default_start_params_emits_headless_slice():
    # `octacam record` has no GUI to POST plugin_params, so the plugin must
    # contribute an arm slice itself (fps + duration_ms) or the board is never
    # armed and the external-triggered cameras hang forever.

    plugin, _ = _plugin_with_fake()
    assert plugin.default_start_params(83.0, 12.0) == {
        "fps": 83,
        "duration_ms": 12000,
        "match_thorimage": False,
    }
    # And it must reach on_recording_start under the "twophoton" key via the manager.
    params = PluginManager([plugin]).default_start_params(80.0, 5.0)
    assert params == {
        "twophoton": {"fps": 80, "duration_ms": 5000, "match_thorimage": False}
    }


def test_on_recording_start_surfaces_write_failure_to_gui():
    # A wedged/closed CDC link makes the arm write silently no-op; the failure
    # must reach the GUI (broadcast + _last_error), not just the server log.
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    PluginManager([plugin]).attach(
        broadcast=lambda topic, data: events.append((topic, data))
    )
    link.fail_writes = True  # link open, but the write never lands
    plugin.on_recording_start({"fps": 100, "duration_ms": 1000})
    assert plugin.last_error and "arm write" in plugin.last_error
    assert any(
        topic == "twophoton_state" and data["error"] == plugin.last_error
        for topic, data in events
    )


def test_on_recording_start_surfaces_ack_timeout_to_gui(monkeypatch):
    plugin, link = _plugin_with_fake()
    link.acks = False
    monkeypatch.setattr(twophoton, "ACK_TIMEOUT_S", 0.05)
    events: list[tuple[str, dict]] = []
    PluginManager([plugin]).attach(
        broadcast=lambda topic, data: events.append((topic, data))
    )
    plugin.on_recording_start({"fps": 100, "duration_ms": 1000})
    assert plugin.last_error and "no arm ack" in plugin.last_error
    assert any(
        topic == "twophoton_state" and data["error"] == plugin.last_error
        for topic, data in events
    )


def test_good_arm_clears_prior_arm_error():
    plugin, _ = _plugin_with_fake()
    plugin.last_error = "arm write to /dev/arduinoCams failed"
    plugin._on_status("A")  # firmware reports armed
    assert plugin.last_error is None


# ---------------------------------------------------------------------------
# Plugin: Arduino status callback and broadcast
# ---------------------------------------------------------------------------


def test_arduino_status_updates_internal_state():
    plugin, _ = _plugin_with_fake()
    assert plugin.board_state == "idle"
    plugin._on_status("A")
    assert plugin.board_state == "armed"
    plugin._on_status("T")
    assert plugin.board_state == "triggered"
    plugin._on_status("D")
    assert plugin.board_state == "done"


def test_broadcast_called_on_status_change():
    plugin, _ = _plugin_with_fake()
    received = []
    PluginManager([plugin]).attach(
        broadcast=lambda kind, payload: received.append((kind, payload))
    )

    plugin._on_status("A")
    assert len(received) == 1
    kind, payload = received[0]
    assert kind == "twophoton_state"
    assert payload["state"] == "armed"
    assert payload["device"] == "/dev/arduinoCams"
    assert payload["ready"] is True

    received.clear()
    plugin._on_status("T")
    assert received[0][0] == "twophoton_state"
    assert received[0][1]["state"] == "triggered"


def test_no_broadcast_when_callback_not_set():
    plugin, _ = _plugin_with_fake()
    plugin._on_status("A")  # must not raise even without a broadcast hook


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
    plugin._on_status("A")
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


def test_reconnect_endpoint_reopens_link(monkeypatch):
    plugin, link = _plugin_with_fake(is_open=False)
    monkeypatch.setattr(plugin, "_open", lambda: None)  # suppress real serial open
    link._open = True  # simulate successful open
    client = _test_client(plugin)
    r = client.post("/api/twophoton/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["device"] == "/dev/arduinoCams"
    # The endpoint exists to report the post-reopen state -- assert it, not just
    # the static device field.
    assert data["ready"] is True
    assert data["error"] is None


def test_reconnect_endpoint_surfaces_failure(monkeypatch):
    plugin, _link = _plugin_with_fake(is_open=False)
    # _open returns the error string and leaves the link closed (port absent).
    monkeypatch.setattr(plugin, "_open", lambda: "could not open /dev/arduinoCams")
    client = _test_client(plugin)
    r = client.post("/api/twophoton/reconnect")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is False
    assert data["error"] == "could not open /dev/arduinoCams"


def test_reconnect_endpoint_accepts_device_override():
    # A {"device": ...} body switches the port before reopening (GUI dropdown).
    plugin, _link = _plugin_with_fake(is_open=False)
    r = _test_client(plugin).post(
        "/api/twophoton/reconnect", json={"device": "/dev/ttyUSB3"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["device"] == "/dev/ttyUSB3"
    assert plugin.configured_device == "/dev/ttyUSB3"


def test_from_payload_clamps_duration_to_uint32():
    # An out-of-range duration must clamp, not blow up to_bytes() with struct.error.
    p = ArmParams.from_payload({"duration_ms": 2**40}, 100, 10_000)
    assert p.duration_ms == 0xFFFF_FFFF
    assert len(p.to_bytes()) == 9  # still packs cleanly


def test_on_recording_start_warns_and_skips_when_link_closed(caplog):
    plugin, link = _plugin_with_fake(is_open=False)
    plugin.on_recording_start({"fps": 100, "duration_ms": 5000})
    assert link.snapshot() == []  # nothing armed
    assert any("is not open" in m for m in caplog.messages)  # warning emitted


def test_on_recording_stop_aborted_resets_state_and_broadcasts():
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    PluginManager([plugin]).attach(
        broadcast=lambda topic, data: events.append((topic, data))
    )
    plugin.board_state = "triggered"
    plugin.on_recording_stop(aborted=True)
    # Firmware goes IDLE silently on cancel; the host must reset+broadcast itself.
    assert plugin.board_state == "idle"
    assert any(
        topic == "twophoton_state"
        and data["state"] == "idle"
        and data["device"] == "/dev/arduinoCams"
        and data["ready"] is True
        for topic, data in events
    )
    assert link.snapshot() == [bytes([CANCEL_MAGIC])]


def test_on_recording_start_warns_when_no_arm_ack(caplog, monkeypatch):
    # With no reader to send 'A', the bounded ack wait elapses and warns rather
    # than letting a silently-dropped arm leave the cameras hanging.
    plugin, link = _plugin_with_fake()
    link.acks = False
    monkeypatch.setattr(twophoton, "ACK_TIMEOUT_S", 0.05)
    plugin.on_recording_start({"fps": 100, "duration_ms": 1000})
    assert link.snapshot()  # the arm packet was still sent
    assert any("no arm acknowledgement" in m for m in caplog.messages)


def test_on_recording_start_no_warning_when_ack_arrives(caplog):
    plugin, link = _plugin_with_fake()
    link.acks = False

    def ack():
        # Deliver the firmware 'A' from another thread, as the reader does.
        if wait_until(link.snapshot, timeout=0.5, interval=0.001):
            link._feed(b"A")

    t = threading.Thread(target=ack)
    t.start()
    plugin.on_recording_start({"fps": 100, "duration_ms": 1000})
    t.join(timeout=2.0)
    assert link._armed.is_set()
    assert not any("no arm acknowledgement" in m for m in caplog.messages)


def test_link_broken_broadcasts_not_ready():
    # When the reader marks the port broken mid-session, the plugin must push a
    # state with ready=False so the GUI disables the arm gate.
    plugin, link = _plugin_with_fake()
    events: list[tuple[str, dict]] = []
    PluginManager([plugin]).attach(
        broadcast=lambda topic, data: events.append((topic, data))
    )
    link._open = False  # reader saw the port die
    plugin._on_link_broken()
    topic, data = events[-1]
    assert topic == "twophoton_state"
    assert data["state"] == "idle"
    assert data["device"] == "/dev/arduinoCams"
    assert data["ready"] is False


# ---------------------------------------------------------------------------
# Factory: from_options
# ---------------------------------------------------------------------------


def test_build_uses_default_device_when_omitted():
    # No device key -> falls back to DEFAULT_DEVICE ("/dev/arduinoCams")
    from octacam.plugins.twophoton import DEFAULT_DEVICE

    plugin = TwoPhotonPlugin.from_options({})
    assert plugin.device == DEFAULT_DEVICE


def test_build_uses_provided_options():
    plugin = TwoPhotonPlugin.from_options(
        {
            "device": "/dev/arduinoCams",
            "baud": 9600,
            "default_fps": 50,
            "default_duration_ms": 3000,
        }
    )
    assert plugin.device == "/dev/arduinoCams"
    assert plugin.baud == 9600
    assert plugin._default_fps == 50
    assert plugin._default_duration_ms == 3000


# ---------------------------------------------------------------------------
# TwoPhotonLink: is_open thread-safety
# ---------------------------------------------------------------------------


def test_is_open_safe_when_serial_is_none():
    # is_open must not raise when close() nulls _serial concurrently.
    link = TwoPhotonLink(lambda s: None, _noop, _noop)
    # _serial starts as None; is_open should return False without AttributeError.
    assert link.is_open is False


# ---------------------------------------------------------------------------
# TwoPhotonLink: real link over a fake serial port (open/close/read/write).
#
# These exercise the actual TwoPhotonLink serial plumbing (reader thread,
# write path, close-join) that the FakeLink-based tests above bypass.
# ---------------------------------------------------------------------------


def _noop():
    pass


class _FakeSerial:
    """Minimal pyserial-Serial double: records writes, hands out queued reads."""

    in_waiting = 0  # each read hands out one queued chunk

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


def _use_fake_serial(monkeypatch, fake: _FakeSerial) -> None:
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: fake)


def test_link_open_starts_reader_and_close_joins(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    link = m.TwoPhotonLink(lambda s: None, _noop, _noop)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    assert link._reader is not None and link._reader.is_alive()
    link.close()
    assert link.is_open is False
    assert link._reader is None


def test_link_arm_writes_packet_and_waits_for_the_ack(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    monkeypatch.setattr(m, "ACK_TIMEOUT_S", 0.05)
    link = m.TwoPhotonLink(lambda s: None, _noop, _noop)
    link.open("/dev/fake", 115200)
    params = m.ArmParams(fps=120, duration_ms=5000)
    assert link.arm(params) == "timeout"  # no board answers
    fake.feed(b"A")
    assert wait_until(link._armed.is_set)
    link.close()
    assert bytes(fake.written) == params.to_bytes()


def test_link_reader_invokes_status_callback(monkeypatch):
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    got: list[str] = []
    link = m.TwoPhotonLink(got.append, _noop, _noop)
    link.open("/dev/fake", 115200)
    fake.feed(b"A")
    fake.feed(b"T")
    fake.feed(b"Z")  # not a status byte -> ignored
    assert wait_until(lambda: got == ["A", "T"])
    link.close()


def test_link_read_error_marks_broken(monkeypatch):
    """A mid-run port failure must flip is_open False so the GUI offers reconnect."""
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    link = m.TwoPhotonLink(lambda s: None, _noop, _noop)
    link.open("/dev/fake", 115200)
    assert link.is_open is True
    fake.raise_on_read = serial.SerialException("device disconnected")
    assert wait_until(lambda: link.is_open is False)
    link.close()  # still safe / idempotent after the link broke


def test_link_close_swallows_shutdown_read_error(monkeypatch):
    """Reader hitting an error during close() exits quietly (no crash, joined)."""
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    link = m.TwoPhotonLink(lambda s: None, _noop, _noop)
    link.open("/dev/fake", 115200)
    # Mimics os.read(None, 1) raising TypeError when the port is nulled under us.
    fake.raise_on_read = TypeError("os.read(None, 1)")
    link.close()
    assert link._reader is None
    assert link.is_open is False


def test_link_reopen_starts_fresh_reader(monkeypatch):
    """Open -> close -> open must start a live reader. The reused _reader_stop
    Event must be cleared so the second reader is not born already-stopped.
    """
    import octacam.plugins.twophoton as m

    created: list[_FakeSerial] = []

    def make_serial(*a, **k):
        fake = _FakeSerial()
        created.append(fake)
        return fake

    monkeypatch.setattr(serial, "Serial", make_serial)
    got: list[str] = []
    link = m.TwoPhotonLink(got.append, _noop, _noop)
    link.open("/dev/fake", 115200)
    link.close()
    link.open("/dev/fake", 115200)  # reopen reuses the same _reader_stop Event
    assert link.is_open is True
    assert link._reader is not None and link._reader.is_alive()
    created[-1].feed(b"A")  # the second port's reader must still process bytes
    assert wait_until(lambda: got == ["A"])
    link.close()


def test_link_broken_invokes_on_broken_callback(monkeypatch):
    """A mid-run read failure fires the on_broken hook so the owner can surface
    the lost link (a clean close must NOT fire it).
    """
    import octacam.plugins.twophoton as m

    fake = _FakeSerial()
    _use_fake_serial(monkeypatch, fake)
    broken: list[bool] = []
    link = m.TwoPhotonLink(lambda s: None, lambda: broken.append(True), _noop)
    link.open("/dev/fake", 115200)
    fake.raise_on_read = serial.SerialException("device disconnected")
    assert wait_until(lambda: broken == [True])
    link.close()


# ---------------------------------------------------------------------------
# Firmware provisioning: identity classification, reader, flash, endpoints
# ---------------------------------------------------------------------------


def _verify_with_banner(plugin, link, banner):
    link.banner = banner
    plugin._identify()


def test_reader_distinguishes_bare_status_from_banner():
    """The reader must emit bare 'A'/'T'/'D' statuses AND capture the newline-
    terminated '2PHOTON ...' banner -- the two never collide because the banner
    starts with '2'.
    """
    statuses: list[str] = []
    link = TwoPhotonLink(statuses.append, _noop, _noop)
    for byte in b"A2PHOTON 1 deadbeef\nT":  # one byte per read, as a slow port
        link._feed(bytes([byte]))
    assert link.identity == "2PHOTON 1 deadbeef"
    assert statuses == ["A", "T"]


def test_identify_current_and_outdated():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, f"2PHOTON 2 {plugin.needed_build}")
    assert plugin.firmware_ok
    assert plugin.firmware_provisioning()["state"] == "current"
    assert plugin.firmware_provisioning()["needs_flash"] is False

    _verify_with_banner(plugin, link, "2PHOTON 2")  # older, no fingerprint
    assert plugin.firmware_ok  # still arms
    assert plugin.firmware_provisioning()["state"] == "outdated"


def test_identify_unidentified_is_not_armed():
    # A silent board may be a protocol-v1 one, which would read the 9-byte arm's
    # extra bytes as commands (cancel or re-arm): it is refused, with the reason.
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, None)  # blank / no identify (old firmware)
    assert not plugin.firmware_ok
    assert "did not identify itself" in plugin.last_error
    plugin.on_recording_start({})
    assert link.snapshot() == []
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "unidentified"
    assert prov["needs_flash"] and not prov["safe_to_auto_flash"]


def test_identify_foreign_board_disables_arming():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "TRIGGERBOX 2 abcd")
    assert plugin.firmware_ok is False
    assert plugin.firmware_provisioning()["state"] == "wrong_board"


def test_flash_firmware_success():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "2PHOTON 2")  # out of date
    link.banner = f"2PHOTON 2 {plugin.needed_build}"  # reports current after flash
    result = plugin.flash_firmware()
    assert result.ok
    assert plugin.firmware_provisioning()["needs_flash"] is False


def test_flash_refused_while_armed():
    plugin, _link = _plugin_with_fake()
    plugin.board_state = "armed"
    result = plugin.flash_firmware()
    assert not result.ok
    assert "armed" in result.message


def test_on_recording_start_refuses_on_incompatible_firmware():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(
        plugin, link, "TRIGGERBOX 2 abcd"
    )  # foreign -> firmware_ok False
    plugin.on_recording_start({"fps": 100, "duration_ms": 5000})
    assert link.snapshot() == []  # nothing armed


def test_firmware_and_flash_endpoints():
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "2PHOTON 2")
    client = _test_client(plugin)
    body = client.get("/api/twophoton/firmware").json()
    assert body["state"] == "outdated" and body["needs_flash"] is True

    link.banner = f"2PHOTON 2 {plugin.needed_build}"
    flashed = client.post("/api/twophoton/flash", json={}).json()
    assert flashed["ok"] is True
    assert flashed["provisioning"]["needs_flash"] is False


# ---------------------------------------------------------------------------
# Match ThorImage: the board ends a synced take when ThorImage stops
# ---------------------------------------------------------------------------


class _ControllerSpy:
    """The controller calls the plugin makes: finish_recording (on its own
    thread) and event, plus the settings it reads the take folder from.
    """

    def __init__(self, save_dir="."):
        self.finished: list[str] = []
        self.threads: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.called = threading.Event()
        self._settings = SimpleNamespace(save_dir=str(save_dir))

    def get_settings(self):
        return self._settings

    def finish_recording(self, by: str) -> None:
        self.finished.append(by)
        self.threads.append(threading.current_thread().name)
        self.called.set()

    def event(self, level: str, message: str) -> None:
        self.events.append((level, message))

    def settle(self) -> None:
        """The finish runs on its own thread: give a (non-)call time to land."""
        self.called.wait(0.5)


def _attach(plugin, save_dir="."):
    spy = _ControllerSpy(save_dir)
    plugin.controller = cast(Any, spy)
    return spy


def test_match_thorimage_is_the_default():
    assert TwoPhotonPlugin().match_thorimage is True
    assert TwoPhotonPlugin.from_options({}).match_thorimage is True
    options = {"match_thorimage": "false"}
    assert TwoPhotonPlugin.from_options(options).match_thorimage is False
    options = {"match_thorimage": False}
    assert TwoPhotonPlugin.from_options(options).match_thorimage is False


def test_from_options_parses_the_match_options():
    build = TwoPhotonPlugin.from_options
    plugin = build({"stop_idle_ms": "1500", "max_duration_s": 900, "source": "/s"})
    assert (plugin.stop_idle_ms, plugin.max_duration_s, plugin.source) == (
        1500,
        900.0,
        "/s",
    )
    # Garbage falls back to the defaults; idle_ms stays within the uint16 wire
    # field and never under 200 ms (between two 2P frames; 0 = fixed duration).
    assert build({"stop_idle_ms": "x"}).stop_idle_ms == DEFAULT_STOP_IDLE_MS
    assert build({"stop_idle_ms": 0}).stop_idle_ms == MIN_STOP_IDLE_MS
    assert build({"stop_idle_ms": 10}).stop_idle_ms == MIN_STOP_IDLE_MS
    assert build({"stop_idle_ms": 10**6}).stop_idle_ms == 0xFFFF


def test_match_arm_sends_the_ceiling_and_idle_timeout():
    plugin, link = _plugin_with_fake(match_thorimage=True)
    plugin.stop_idle_ms = 2000
    plugin.max_duration_s = 600.0
    # The Record tab's duration is ignored: ThorImage decides; 600 s is the cap.
    plugin.on_recording_start({"fps": 100, "duration_ms": 5000})
    magic, fps, dur, idle = struct.unpack(ARM_FORMAT, link.snapshot()[0])
    assert (magic, fps, dur, idle) == (ARM_MAGIC, 100, 600_000, 2000)
    assert plugin.take_info() == {"armed": True, "match_thorimage": True}


def test_fixed_duration_arm_disables_idle_detection():
    plugin, link = _plugin_with_fake(match_thorimage=False)
    plugin.on_recording_start({"fps": 100, "duration_ms": 5000})
    _, _, dur, idle = struct.unpack(ARM_FORMAT, link.snapshot()[0])
    assert (dur, idle) == (5000, 0)


def test_the_record_tab_checkbox_overrides_the_config_default():
    plugin, link = _plugin_with_fake(match_thorimage=True)
    params = {"fps": 100, "duration_ms": 5000, "match_thorimage": False}
    assert plugin.take_ceiling_s(params) is None
    plugin.on_recording_start(params)
    _, _, dur, idle = struct.unpack(ARM_FORMAT, link.snapshot()[0])
    assert (dur, idle) == (5000, 0)
    # ...and the other way round.
    plugin, link = _plugin_with_fake(match_thorimage=False)
    params = {"fps": 100, "duration_ms": 5000, "match_thorimage": True}
    assert plugin.take_ceiling_s(params) == plugin.max_duration_s
    plugin.on_recording_start(params)
    assert struct.unpack(ARM_FORMAT, link.snapshot()[0])[3] == plugin.stop_idle_ms


def test_take_ceiling_only_when_armed_and_matching():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    assert plugin.take_ceiling_s(None) is None  # not armed
    assert plugin.take_ceiling_s({}) == plugin.max_duration_s
    manager = PluginManager([plugin])
    assert manager.take_ceiling({"twophoton": {}}) == (
        "twophoton",
        plugin.max_duration_s,
    )
    assert manager.take_ceiling({"flywheel": {}}) is None


def test_an_explicit_duration_is_a_fixed_length_take():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    assert plugin.default_start_params(100.0, 30.0)["match_thorimage"] is True
    slice_ = plugin.default_start_params(100.0, 30.0, fixed_duration=True)
    assert slice_["match_thorimage"] is False
    assert plugin.take_ceiling_s(slice_) is None


def test_done_after_trigger_finishes_a_synced_take():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    spy = _attach(plugin)
    plugin.on_recording_start({})
    for status in "AT":
        plugin._on_status(status)
    assert spy.finished == []
    plugin._on_stop_report(
        {"reason": "thorimage", "edges": 4500, "high_ms": 293_000, "run_ms": 294_000}
    )
    plugin._on_status("D")
    spy.settle()
    assert spy.finished == ["twophoton"]  # a completed take, not an early stop
    # Off the serial reader: finish_recording takes the controller lock.
    assert spy.threads == ["twophoton-finish"]
    assert plugin.take_info()["stop"]["reason"] == "thorimage"


def test_done_does_not_end_a_fixed_duration_take():
    plugin, _ = _plugin_with_fake(match_thorimage=False)
    spy = _attach(plugin)
    plugin.on_recording_start({})
    for status in "ATD":
        plugin._on_status(status)
    spy.settle()
    assert spy.finished == []  # the controller's own deadline ends it


def test_a_stale_done_never_ends_a_later_take():
    # A 'D' after on_recording_stop reset the state (or before the next take's
    # board triggered) must not end the take now waiting.
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    spy = _attach(plugin)
    plugin.on_recording_start({})
    plugin._on_status("A")
    plugin._on_status("D")  # never saw 'T'
    plugin.on_recording_stop(aborted=False)
    plugin._on_status("D")
    spy.settle()
    assert spy.finished == []


def test_a_new_take_clears_the_previous_stop_report():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    plugin.on_recording_start({})
    plugin.board_state = "triggered"
    plugin._on_stop_report({"reason": "ceiling", "edges": 1, "high_ms": 5, "run_ms": 9})
    assert "stop" in plugin.take_info()
    plugin.on_recording_start({})
    assert "stop" not in plugin.take_info()


def test_a_late_stop_report_never_lands_in_the_next_take():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    plugin.on_recording_start({})
    plugin._on_status("A")  # armed, not triggered yet
    plugin._on_stop_report(
        {"reason": "thorimage", "edges": 1, "high_ms": 1, "run_ms": 1}
    )
    assert "stop" not in plugin.take_info()


def test_parse_stop_report():
    assert parse_stop_report("STOP I 4500 293000 294100") == {
        "reason": "thorimage",
        "edges": 4500,
        "high_ms": 293000,
        "run_ms": 294100,
    }
    assert (parse_stop_report("STOP C 1 3600000 3600000") or {})["reason"] == "ceiling"
    for garbled in ("STOP X 1 2 3", "STOP I 1 2", "STOP I a b c", "STOPI 1 2 3"):
        assert parse_stop_report(garbled) is None


def test_the_reader_delivers_the_stop_report_before_done():
    """The "STOP ..." line starts with 'S' (never a status byte), so it is read
    as a line and delivered before the trailing 'D'.
    """
    events: list = []
    link = TwoPhotonLink(
        lambda s: events.append(("status", s)),
        _noop,
        lambda r: events.append(("stop", r["reason"], r["edges"])),
    )
    for byte in b"ATSTOP I 4500 293000 294100\nD":  # one byte per read
        link._feed(bytes([byte]))
    assert events == [
        ("status", "A"),
        ("status", "T"),
        ("stop", "thorimage", 4500),
        ("status", "D"),
    ]


def test_v1_firmware_is_refused_and_offered_a_reflash():
    # A v1 board would misparse the 9-byte arm packet: refuse to arm it.
    plugin, link = _plugin_with_fake()
    _verify_with_banner(plugin, link, "2PHOTON 1 deadbeef")
    assert plugin.firmware_ok is False
    prov = plugin.firmware_provisioning()
    assert prov["state"] == "wrong_version"
    assert prov["needs_flash"]
    plugin.on_recording_start({})
    assert link.snapshot() == []


def test_status_reports_the_match_config():
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    status = plugin.status()
    assert status["match_thorimage"] is True
    assert status["max_duration_s"] == plugin.max_duration_s
    assert status["stop_idle_ms"] == plugin.stop_idle_ms


def test_the_take_info_lands_in_the_take_folder(tmp_path):
    # process pairs a take with its 2P data from this file (there is no plugin
    # section in develop's summary).
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    _attach(plugin, tmp_path)
    plugin.on_recording_start({})
    plugin._on_status("A")
    plugin._on_status("T")
    report = {"reason": "thorimage", "edges": 9, "high_ms": 8, "run_ms": 7}
    plugin._on_stop_report(report)
    plugin.on_recording_stop(aborted=False)
    info = json.loads((tmp_path / "octacam_recording" / "twophoton.json").read_text())
    assert info == {"armed": True, "match_thorimage": True, "stop": report}


def test_an_unarmed_take_is_recorded_as_such(tmp_path):
    plugin, _ = _plugin_with_fake()
    _attach(plugin, tmp_path)
    plugin.on_recording_start(None)
    plugin.on_recording_stop(aborted=True)
    info = json.loads((tmp_path / "octacam_recording" / "twophoton.json").read_text())
    assert info == {"armed": False, "match_thorimage": False}


# ---------------------------------------------------------------------------
# ThorSync duration check: the GUI says when ThorSync would stop too early
# ---------------------------------------------------------------------------


def _thorsync_share(tmp_path, duration):
    folder = tmp_path / "share" / "PAM7xCI80" / "SyncData184"
    folder.mkdir(parents=True)
    (folder / "ThorRealTimeDataSettings.xml").write_text(
        '<?xml version="1.0"?><ThorData>'
        f'<AcquireBoard active="1" duration="{duration}">'
        '<SampleRate enable="1" rate="100000" /></AcquireBoard></ThorData>'
    )
    return tmp_path / "share"


def _thorimage_plan(share, name, timepoints, frame_rate=7.7):
    folder = share / "PAM7xCI80" / name
    folder.mkdir(parents=True)
    (folder / "Experiment.xml").write_text(
        f'<?xml version="1.0"?><ThorImageExperiment><LSM frameRate="{frame_rate}" />'
        f'<Timelapse timepoints="{timepoints}" /></ThorImageExperiment>'
    )


def test_the_thorsync_limit_is_read_off_thread_and_reported(tmp_path):
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    plugin.source = str(_thorsync_share(tmp_path, 320))
    plugin.refresh_thorsync_limit(wait=True)
    assert plugin.status()["thorsync"] == {
        "folder": "PAM7xCI80/SyncData184",
        "limit_s": 320.0,
        "planned_folder": None,  # no ThorImage folder on this share
        "planned_s": None,
    }
    warning = plugin.thorsync_warning(3600)
    assert warning
    assert "stop after 320 s" in warning
    assert "60 min" in warning
    assert plugin.thorsync_warning(300) is None  # a short take fits


def test_arming_a_take_longer_than_thorsync_warns_the_operator(tmp_path):
    plugin, link = _plugin_with_fake(match_thorimage=True)
    spy = _attach(plugin, tmp_path)
    plugin.source = str(_thorsync_share(tmp_path, 320))
    plugin.refresh_thorsync_limit(wait=True)
    plugin.on_recording_start({"fps": 100, "match_thorimage": True})
    assert link.written  # still armed: the warning never blocks a take
    ((level, message),) = spy.events
    assert level == "warning"
    assert "ThorSync is set to stop after 320 s" in message


def test_no_thorsync_warning_when_the_limit_covers_the_take(tmp_path):
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    spy = _attach(plugin, tmp_path)
    plugin.source = str(_thorsync_share(tmp_path, 7200))
    plugin.refresh_thorsync_limit(wait=True)
    plugin.on_recording_start({"fps": 100, "match_thorimage": True})
    assert spy.events == []


def test_a_synced_take_is_compared_with_thorimage_s_own_plan(tmp_path):
    # Comparing synced takes with the 60 min ceiling warned on every take
    # whenever ThorSync was set below an hour. ThorImage's own plan decides.
    share = _thorsync_share(tmp_path, 320)
    _thorimage_plan(share, "Fly5_Streaming", 3000)  # 3000 / 7.7 Hz = 390 s
    plugin, _ = _plugin_with_fake(match_thorimage=True)
    plugin.source = str(share)
    plugin.refresh_thorsync_limit(wait=True)
    warning = plugin.thorsync_warning(3600, synced=True) or ""
    assert "acquire 390 s (read from PAM7xCI80/Fly5_Streaming)" in warning
    assert "stop after 320 s" in warning

    short = _thorsync_share(tmp_path / "b", 320)
    _thorimage_plan(short, "Fly6_Streaming", 1500)  # 195 s: fits in 320 s
    plugin.source = str(short)
    plugin.refresh_thorsync_limit(wait=True)
    assert plugin.thorsync_warning(3600, synced=True) is None
    assert plugin.thorsync_warning(3600, synced=False) is not None  # a 1 h take
