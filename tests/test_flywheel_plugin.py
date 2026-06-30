"""Flywheel plugin: command wire format + plugin hooks."""

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from octacam.plugins.flywheel import (
    COMMAND_FIELDS,
    JOG_DEFAULT_INTERVAL_US,
    JOG_MAX_INTERVAL_US,
    JOG_MIN_INTERVAL_US,
    Command,
    FlywheelPlugin,
    JogClock,
    _clamp_jog_interval_us,
)


class FakeLink:
    """Stand-in for SerialLink that records writes (no pyserial needed)."""

    def __init__(self, is_open=True):
        self._open = is_open
        self._lock = threading.Lock()  # writes arrive from the jog clock thread
        self.written: list[bytes] = []

    @property
    def is_open(self) -> bool:
        return self._open

    def write_command(self, command: Command) -> None:
        with self._lock:
            if self._open:  # mirror SerialLink: writes no-op once closed
                self.written.append(command.to_bytes())

    def open(self, device, baud) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def snapshot(self) -> list[bytes]:
        with self._lock:
            return list(self.written)


def _wait(predicate, timeout=1.0):
    """Poll until predicate() is true (jog start/stop are non-blocking)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ------------------------------------------------------------- wire format


def test_command_wire_format_matches_cpp_packed_struct():
    # Hand-computed little-endian layout of the packed C++ struct:
    # int16 n_steps, uint16 step_interval_us, uint16 rest_duration_ms,
    # uint8 n_repeats, uint8 init_wait_duration_s -> 8 bytes total.
    command = Command(
        n_steps=-4096,  # 0xF000
        step_interval_us=1465,  # 0x05B9
        rest_duration_ms=1000,  # 0x03E8
        n_repeats=3,
        init_wait_duration_s=10,
    )
    assert command.to_bytes() == b"\x00\xf0\xb9\x05\xe8\x03\x03\x0a"
    assert len(Command().to_bytes()) == 8


def test_single_step_commands():
    assert Command(n_steps=1).to_bytes() == b"\x01\x00\x00\x00\x00\x00\x00\x00"
    assert Command(n_steps=-1).to_bytes() == b"\xff\xff\x00\x00\x00\x00\x00\x00"
    assert Command(n_steps=0).to_bytes() == b"\x00" * 8


def test_command_from_payload():
    payload = dict.fromkeys(COMMAND_FIELDS, 1)
    assert Command.from_payload(payload) == Command(*([1] * 5))


# --------------------------------------------------------- recording hooks


def test_on_first_frame_writes_armed_command():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    plugin.on_first_frame(
        {
            "flywheel": {
                "n_steps": -4096,
                "step_interval_us": 1465,
                "rest_duration_ms": 1000,
                "n_repeats": 3,
                "init_wait_duration_s": 10,
            }
        }
    )
    assert link.written == [b"\x00\xf0\xb9\x05\xe8\x03\x03\x0a"]


def test_on_first_frame_without_params_is_noop():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    plugin.on_first_frame(None)
    plugin.on_first_frame({})
    plugin.on_first_frame({"other_plugin": {"n_steps": 1}})
    plugin.on_first_frame({"flywheel": {"bogus": "field"}})  # malformed -> skipped
    assert link.written == []


# --------------------------------------------------------- jog pulse clock


RELEASE = Command(n_steps=0).to_bytes()


def _start_jog(plugin, direction, interval_us=JOG_MIN_INTERVAL_US, client_id=1):
    return plugin.on_ws_message(
        {
            "type": "jog",
            "action": "start",
            "direction": direction,
            "interval_us": interval_us,
        },
        client_id,
    )


def _stop_jog(plugin, client_id=1):
    return plugin.on_ws_message({"type": "jog", "action": "stop"}, client_id)


def _released(link):
    """True once the clock has stopped and written its coil-release."""
    return _wait(lambda: link.snapshot()[-1:] == [RELEASE])


def test_clamp_jog_interval():
    assert _clamp_jog_interval_us(JOG_MIN_INTERVAL_US - 1) == JOG_MIN_INTERVAL_US
    assert _clamp_jog_interval_us(JOG_MAX_INTERVAL_US + 1) == JOG_MAX_INTERVAL_US
    assert _clamp_jog_interval_us(2000) == 2000
    assert _clamp_jog_interval_us(None) == JOG_DEFAULT_INTERVAL_US
    assert _clamp_jog_interval_us("nope") == JOG_DEFAULT_INTERVAL_US


def test_jog_start_pulses_until_stop_then_releases():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    assert _start_jog(plugin, 1) is True
    time.sleep(0.03)  # let the backend clock emit several pulses
    assert _stop_jog(plugin) is True
    assert _released(link)  # coils released after the (async) stop

    writes = link.snapshot()
    assert len(writes) >= 2  # at least one pulse + the release
    assert writes[-1] == RELEASE
    # Every tick before the release is a single forward half-step.
    assert all(w == Command(n_steps=1).to_bytes() for w in writes[:-1])


def test_jog_direction_sign():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, -1)
    assert _wait(lambda: link.snapshot()[:1] == [Command(n_steps=-1).to_bytes()])
    _stop_jog(plugin)


def test_jog_ignores_bad_direction():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    assert _start_jog(plugin, 7) is True  # handled, but not a valid direction
    time.sleep(0.02)
    assert link.snapshot() == []


def test_jog_noop_when_serial_closed():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink(is_open=False)
    assert _start_jog(plugin, 1) is True
    time.sleep(0.02)
    assert link.snapshot() == []


def test_jog_stopped_by_owner_ws_disconnect():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1, client_id=7)
    time.sleep(0.02)
    plugin.on_ws_disconnect(7)  # owner's socket dropped -> must stop the motor
    assert _released(link)
    n = len(link.snapshot())
    time.sleep(0.02)
    assert len(link.snapshot()) == n  # clock really stopped — no further pulses


def test_jog_restart_switches_direction():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1)
    time.sleep(0.02)
    _start_jog(plugin, -1)  # re-press the other way without an explicit stop
    time.sleep(0.02)
    _stop_jog(plugin)
    assert _released(link)
    writes = link.snapshot()
    assert writes[0] == Command(n_steps=1).to_bytes()
    assert writes[-1] == RELEASE
    assert Command(n_steps=-1).to_bytes() in writes
    # The superseded forward thread must not have injected a stray release.
    assert writes.count(RELEASE) == 1


# ---- multi-client jog ownership (one shared motor, many browsers) ----


def test_jog_stop_ignored_from_non_owner():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1, client_id=1)  # operator A holds the button
    time.sleep(0.02)
    _stop_jog(plugin, client_id=2)  # operator B releases -> must NOT stop A
    time.sleep(0.02)
    assert RELEASE not in link.snapshot()  # still jogging
    _stop_jog(plugin, client_id=1)  # A releases -> stops
    assert _released(link)


def test_jog_other_client_disconnect_keeps_it_running():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1, client_id=1)
    time.sleep(0.02)
    plugin.on_ws_disconnect(2)  # an unrelated browser drops
    time.sleep(0.02)
    assert RELEASE not in link.snapshot()
    n = len(link.snapshot())
    assert _wait(lambda: len(link.snapshot()) > n)  # A still being pulsed
    plugin.on_ws_disconnect(1)  # the owner drops -> stops
    assert _released(link)


def test_jog_takeover_by_second_client():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1, client_id=1)
    time.sleep(0.02)
    _start_jog(plugin, -1, client_id=2)  # B seizes the shared motor
    time.sleep(0.02)
    _stop_jog(plugin, client_id=1)  # A (no longer owner) releases -> ignored
    time.sleep(0.02)
    writes = link.snapshot()
    assert RELEASE not in writes  # B still jogging
    assert Command(n_steps=-1).to_bytes() in writes
    _stop_jog(plugin, client_id=2)  # the new owner releases
    assert _released(link)


def test_teardown_stops_jog_and_releases_before_close():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    _start_jog(plugin, 1, client_id=1)
    time.sleep(0.02)
    plugin.teardown()  # joins, flushes the release, then closes the link
    assert link.snapshot()[-1] == RELEASE  # release landed while still open
    assert not link.is_open


def test_jog_start_refused_while_closing():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink()
    plugin.teardown()  # marks the plugin closing (and closes the link)
    link.open(None, None)  # pretend the port came back after teardown
    assert _start_jog(plugin, 1) is True  # message is handled...
    time.sleep(0.02)
    assert link.snapshot() == []  # ...but no jog is spawned while closing
    assert plugin._jog_owner is None


class StallingLink(FakeLink):
    """FakeLink whose first write blocks until released — simulates a serial
    write wedged past the teardown join timeout."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write_command(self, command):
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(2.0)  # bounded so a broken test can't hang
        super().write_command(command)


def test_stop_join_reports_wedged_thread(monkeypatch):
    # A thread wedged in a write past the join timeout cannot flush its release,
    # so stop(join=True) returns False and teardown must release the coils itself.
    monkeypatch.setattr(JogClock, "JOIN_TIMEOUT_S", 0.05)
    plugin = FlywheelPlugin()
    plugin._link = link = StallingLink()
    try:
        _start_jog(plugin, 1)
        assert link.entered.wait(1.0)  # clock thread is wedged in the first write
        assert plugin._jog.stop(join=True) is False
    finally:
        link.release.set()  # let the wedged thread finish and exit cleanly


def test_non_jog_message_not_handled():
    plugin = FlywheelPlugin()
    plugin._link = FakeLink()
    assert plugin.on_ws_message({"type": "something-else"}, 1) is False


# ------------------------------------------------------ contributed router


def _client(plugin) -> TestClient:
    app = FastAPI()
    app.include_router(plugin.api_router())
    return TestClient(app)


def test_serial_command_endpoint_503_when_closed():
    plugin = FlywheelPlugin()
    plugin._link = FakeLink(is_open=False)
    response = _client(plugin).post(
        "/api/serial/command", json=dict.fromkeys(COMMAND_FIELDS, 1)
    )
    assert response.status_code == 503


def test_serial_command_endpoint_writes_when_open():
    plugin = FlywheelPlugin()
    plugin._link = link = FakeLink(is_open=True)
    response = _client(plugin).post(
        "/api/serial/command", json=dict.fromkeys(COMMAND_FIELDS, 2)
    )
    assert response.status_code == 200
    assert link.written == [Command(*([2] * 5)).to_bytes()]


def test_serial_command_endpoint_422_on_bad_payload():
    plugin = FlywheelPlugin()
    plugin._link = FakeLink(is_open=True)
    response = _client(plugin).post("/api/serial/command", json={"n_steps": 1})
    assert response.status_code == 422
