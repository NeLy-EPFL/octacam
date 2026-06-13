"""Arduino plugin: command wire format + plugin hooks."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from octacam.plugins.arduino import COMMAND_FIELDS, ArduinoPlugin, Command


class FakeLink:
    """Stand-in for SerialLink that records writes (no pyserial needed)."""

    def __init__(self, is_open=True):
        self._open = is_open
        self.written: list[bytes] = []

    @property
    def is_open(self) -> bool:
        return self._open

    def write_command(self, command: Command) -> None:
        self.written.append(command.to_bytes())

    def open(self, device, baud) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False


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
    plugin = ArduinoPlugin()
    plugin._link = link = FakeLink()
    plugin.on_first_frame(
        {
            "arduino": {
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
    plugin = ArduinoPlugin()
    plugin._link = link = FakeLink()
    plugin.on_first_frame(None)
    plugin.on_first_frame({})
    plugin.on_first_frame({"other_plugin": {"n_steps": 1}})
    plugin.on_first_frame({"arduino": {"bogus": "field"}})  # malformed -> skipped
    assert link.written == []


# ------------------------------------------------------------ jog over WS


def test_on_ws_message_jog():
    plugin = ArduinoPlugin()
    plugin._link = link = FakeLink()
    assert plugin.on_ws_message({"type": "jog", "n_steps": 1}) is True
    assert plugin.on_ws_message({"type": "jog", "n_steps": -1}) is True
    assert plugin.on_ws_message({"type": "jog", "n_steps": 7}) is True  # clamped out
    assert plugin.on_ws_message({"type": "something-else"}) is False
    assert link.written == [
        Command(n_steps=1).to_bytes(),
        Command(n_steps=-1).to_bytes(),
    ]


# ------------------------------------------------------ contributed router


def _client(plugin) -> TestClient:
    app = FastAPI()
    app.include_router(plugin.api_router())
    return TestClient(app)


def test_serial_command_endpoint_503_when_closed():
    plugin = ArduinoPlugin()
    plugin._link = FakeLink(is_open=False)
    response = _client(plugin).post(
        "/api/serial/command", json=dict.fromkeys(COMMAND_FIELDS, 1)
    )
    assert response.status_code == 503


def test_serial_command_endpoint_writes_when_open():
    plugin = ArduinoPlugin()
    plugin._link = link = FakeLink(is_open=True)
    response = _client(plugin).post(
        "/api/serial/command", json=dict.fromkeys(COMMAND_FIELDS, 2)
    )
    assert response.status_code == 200
    assert link.written == [Command(*([2] * 5)).to_bytes()]


def test_serial_command_endpoint_422_on_bad_payload():
    plugin = ArduinoPlugin()
    plugin._link = FakeLink(is_open=True)
    response = _client(plugin).post("/api/serial/command", json={"n_steps": 1})
    assert response.status_code == 422
