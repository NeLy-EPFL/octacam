"""Arduino stepper-motor controller plugin (opt-in).

Drives the Arduino stepper over a serial link. Enable it with a ``plugins:``
entry in ``octacam_config.yml``::

    plugins:
      - arduino:
          device: /dev/ttyACM0
          baud: 115200

or with ``octacam gui --plugin arduino``. Requires the optional dependency:
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
import time
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

# Hold-to-jog pulse timing. The jog clock writes one single-half-step command
# per tick, so the step interval is bounded below by the time it takes to send
# an 8-byte command at the serial baud rate (~0.7 ms at 115200) — going faster
# just saturates the link. 65535 µs is the wire-format ceiling for the field.
JOG_MIN_INTERVAL_US = 1000
JOG_MAX_INTERVAL_US = 65535
JOG_DEFAULT_INTERVAL_US = 2000
# Safety backstop: stop a jog after this many half-steps even if the release
# message never arrives (lost message, frozen tab). The primary stop is the
# button release or a WebSocket disconnect; this only bounds a runaway
# (~24 revolutions of a 4096-half-step motor).
JOG_MAX_STEPS = 100_000

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
    def from_payload(cls, payload) -> Command:
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
            raise RuntimeError("pyserial not installed: pip install octacam[arduino]")
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
            except serial.SerialException as e:  # pyright: ignore[reportOptionalMemberAccess]
                log.warning("Serial write failed: %s", e)


def _clamp_jog_interval_us(value) -> int:
    """Clamp a requested jog step interval into the supported µs range.

    Falls back to the default for missing or non-numeric input so a malformed
    jog message still produces a usable (rather than zero/blocking) tick rate.
    """
    try:
        us = int(value)
    except (TypeError, ValueError):
        return JOG_DEFAULT_INTERVAL_US
    return max(JOG_MIN_INTERVAL_US, min(JOG_MAX_INTERVAL_US, us))


class JogClock:
    """Backend pulse clock for hold-to-jog position adjustment.

    Pressing a CCW/CW button starts the clock; releasing it stops the clock.
    While running, a dedicated thread writes one single-half-step command per
    tick at a fixed interval, so the step frequency is set by a real clock in
    the backend rather than by the rate of inbound WebSocket messages (which is
    coarse, jittery, and capped by round-trip latency). Stopping releases the
    motor coils with a final ``n_steps=0`` command.

    ``write`` must be a callable taking a :class:`Command`; it is shared with
    the loop/first-frame writers, which serialise on the link's own lock.
    """

    def __init__(self, write, max_steps: int = JOG_MAX_STEPS):
        self._write = write
        self._max_steps = max_steps
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, direction: int, interval_us: object) -> None:
        interval_s = _clamp_jog_interval_us(interval_us) / 1_000_000
        with self._lock:
            self._stop_locked()
            self._stop = stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(direction, interval_s, stop),
                name="arduino-jog",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        # Bounded so a write stuck on the serial write timeout can't wedge the
        # caller (a web executor thread). A timed-out thread releases the coils
        # and exits on its own; the link lock keeps writes from interleaving.
        self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self, direction: int, interval_s: float, stop: threading.Event) -> None:
        command = Command(n_steps=direction)
        release = Command(n_steps=0)
        steps = 0
        next_tick = time.monotonic()
        try:
            while not stop.is_set() and steps < self._max_steps:
                self._write(command)
                steps += 1
                next_tick += interval_s
                now = time.monotonic()
                delay = next_tick - now
                if delay > 0:
                    if stop.wait(delay):
                        break
                else:
                    # Falling behind (link saturated): re-anchor and keep the
                    # stop check responsive instead of accumulating drift.
                    next_tick = now
            else:
                if steps >= self._max_steps:
                    log.warning(
                        "Arduino jog: hit %d-step safety cap; stopping", self._max_steps
                    )
        finally:
            self._write(release)


@register("arduino")
def _build(options: dict) -> ArduinoPlugin:
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
        # Bound method (not self._link.write_command) so the clock always
        # writes through the current link, even after a reconnect swaps it.
        self._jog = JogClock(self._write)

    def _write(self, command: Command) -> None:
        self._link.write_command(command)

    # ---------------------------------------------------- process lifecycle

    def setup(self) -> None:
        self._open()

    def _open(self) -> str | None:
        """(Re)open the serial link, returning an error message on failure (else
        None). Never raises: a missing board must not stop the GUI launching,
        and it can be retried at runtime via the reconnect endpoint once the
        board is plugged in."""
        try:
            self._link.open(self.device, self.baud)
        except Exception as e:
            log.warning("Arduino plugin: failed to open %s: %s", self.device, e)
            return str(e)
        log.info("Arduino plugin: opened %s @ %d", self.device, self.baud)
        return None

    def teardown(self) -> None:
        self._jog.stop()
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

    def _command_from(self, params: dict | None) -> Command | None:
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

        @router.post("/api/serial/reconnect")
        def serial_reconnect():
            """Re-attempt opening the serial port.

            Lets the operator recover from a board that was unplugged or absent
            at launch (and is now connected) without restarting the server. The
            response carries the resulting ``ready`` state so the GUI can flip
            the Arduino tab from its "serial unavailable" notice to usable.
            """
            error = self._open()
            return {"ready": self._link.is_open, "device": self.device, "error": error}

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
        """Handle hold-to-jog start/stop messages.

        ``{"type": "jog", "action": "start", "direction": -1|1,
        "interval_us": N}`` starts the backend pulse clock in that direction;
        ``{"type": "jog", "action": "stop"}`` stops it. The clock — not these
        messages — paces the steps, so the client sends exactly one of each per
        hold.
        """
        if message.get("type") != "jog":
            return False
        if message.get("action") == "start":
            if message.get("direction") in (-1, 1) and self._link.is_open:
                self._jog.start(message["direction"], message.get("interval_us"))
        else:  # "stop" (or any non-start jog message) halts the clock
            self._jog.stop()
        return True

    def on_ws_disconnect(self) -> None:
        # A dropped control socket must not leave the motor spinning: the
        # release message can't arrive over a closed connection.
        self._jog.stop()
