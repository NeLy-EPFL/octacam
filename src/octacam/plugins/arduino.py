"""Arduino stepper-motor controller plugin (opt-in).

Drives the Arduino stepper over a serial link. Enable it with a ``plugins:``
entry in ``octacam_config.yml``::

    plugins:
      - arduino:
          device: /dev/ttyACM0
          baud: 115200

or with ``octacam serve --plugin arduino``. Requires the optional dependency:
``pip install octacam[arduino]``.

It contributes:
  * an ``on_first_frame`` hook that fires an armed loop command at the first
    captured frame (so the stepper motion is synchronised to actual capture),
  * a ``POST /api/serial/command`` endpoint to run a loop on demand,
  * a "jog" WebSocket handler for hold-to-step position adjustment.
"""

from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass

from octacam.plugins import register
from octacam.plugins.base import Plugin

try:
    import serial
except ImportError:  # pyserial is the `arduino` extra; core runs without it
    serial = None  # type: ignore[assignment]

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 115200

# Wire format of the packed C++ Command struct (and the matching struct in
# arduino_script): little-endian int16, uint16, uint16, uint8, uint8.
_COMMAND_FORMAT = "<hHHBB"
COMMAND_FIELDS = (
    "n_steps",
    "step_interval_us",
    "rest_duration_ms",
    "n_repeats",
    "init_wait_duration_s",
)


@dataclass
class Command:
    n_steps: int = 0
    step_interval_us: int = 0
    rest_duration_ms: int = 0
    n_repeats: int = 0
    init_wait_duration_s: int = 0

    def to_bytes(self) -> bytes:
        return struct.pack(
            _COMMAND_FORMAT,
            self.n_steps,
            self.step_interval_us,
            self.rest_duration_ms,
            self.n_repeats,
            self.init_wait_duration_s,
        )

    @classmethod
    def from_payload(cls, payload) -> "Command":
        """Build a Command from a dict of integer fields.

        Raises KeyError/TypeError/ValueError on malformed input; callers map
        that to the appropriate error (HTTP 422, a warning, ...).
        """
        return cls(**{field: int(payload[field]) for field in COMMAND_FIELDS})


class SerialLink:
    """Thread-safe transport to the Arduino over a serial port.

    ``write_command`` may be called concurrently from the controller's monitor
    thread (first-frame) and a web executor thread (jog), so writes are
    serialised by a lock.
    """

    def __init__(self):
        self._serial = None
        self._lock = threading.Lock()

    def open(self, device: str, baud: int) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial not installed: pip install octacam[arduino]"
            )
        self.close()
        self._serial = serial.Serial(device, baud, timeout=0.1, write_timeout=1)

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def write_command(self, command: Command) -> None:
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                return
            try:
                self._serial.write(command.to_bytes())
            except serial.SerialException as e:
                log.warning("Serial write failed: %s", e)


@register("arduino")
def _build(options: dict) -> "ArduinoPlugin":
    if serial is None:
        raise RuntimeError("pyserial not installed: pip install octacam[arduino]")
    device = str(options.get("device", DEFAULT_DEVICE))
    try:
        baud = int(options.get("baud", DEFAULT_BAUD))
    except (TypeError, ValueError):
        log.warning(
            "Arduino plugin: invalid baud %r; using %d",
            options.get("baud"),
            DEFAULT_BAUD,
        )
        baud = DEFAULT_BAUD
    return ArduinoPlugin(device=device, baud=baud)


class ArduinoPlugin(Plugin):
    name = "arduino"

    def __init__(self, device: str = DEFAULT_DEVICE, baud: int = DEFAULT_BAUD):
        self.device = device
        self.baud = baud
        self._link = SerialLink()

    # ---------------------------------------------------- process lifecycle

    def setup(self) -> None:
        try:
            self._link.open(self.device, self.baud)
            log.info("Arduino plugin: opened %s @ %d", self.device, self.baud)
        except Exception as e:
            log.warning("Arduino plugin: failed to open %s: %s", self.device, e)

    def teardown(self) -> None:
        self._link.close()

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        return {"device": self.device}

    # -------------------------------------------------- recording lifecycle

    def on_first_frame(self, params: dict | None) -> None:
        command = self._command_from(params)
        if command is not None:
            self._link.write_command(command)

    def _command_from(self, params: dict | None) -> "Command | None":
        if not params:
            return None
        spec = params.get(self.name)
        if not spec:
            return None
        try:
            return Command.from_payload(spec)
        except (KeyError, TypeError, ValueError):
            log.warning("Arduino plugin: ignoring invalid command %r", spec)
            return None

    # --------------------------------------------------------- web contrib

    def api_router(self):
        from fastapi import APIRouter, Body, HTTPException

        router = APIRouter()

        @router.post("/api/serial/command")
        def serial_command(payload: dict = Body(...)):
            if not self._link.is_open:
                raise HTTPException(503, "Serial port not available")
            try:
                command = Command.from_payload(payload)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "Invalid stepper command") from None
            self._link.write_command(command)
            return {"status": "ok"}

        return router

    def on_ws_message(self, message: dict) -> bool:
        if message.get("type") != "jog":
            return False
        n_steps = message.get("n_steps")
        if n_steps in (-1, 0, 1) and self._link.is_open:
            self._link.write_command(Command(n_steps=n_steps))
        return True
