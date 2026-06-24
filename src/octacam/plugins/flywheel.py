"""Flywheel stepper-motor controller plugin (opt-in).

Drives the Arduino stepper over a serial link. Enable it with a ``[[plugins]]``
entry in ``octacam_config.toml`` (settings go under a ``[plugins.options]``
sub-table)::

    [[plugins]]
    name = "flywheel"

    [plugins.options]
    device = "/dev/ttyACM0"
    baud = 115200

or with ``octacam gui --plugin flywheel``. Its serial dependency (pyserial)
ships with octacam by default, so no extra install is needed.

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
except ImportError:  # pyserial ships by default; guard against a broken env
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
# arduino/stepper_motor): little-endian int16, uint16, uint16, uint8, uint8.
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
        # Serializes open/close/reconnect so two concurrent reconnects (e.g. a
        # double-clicked Reconnect button) cannot both create a port and leak
        # the loser's file descriptor.
        self._lifecycle_lock = threading.Lock()

    def open(self, device: str, baud: int) -> None:
        if serial is None:
            raise RuntimeError("pyserial not installed: pip install pyserial")
        with self._lifecycle_lock:
            self._close_locked()
            self._serial = serial.Serial(device, baud, timeout=0.1, write_timeout=1)

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Close the port. Caller must hold ``_lifecycle_lock``."""
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

    ``start``/``stop`` never block on the serial link: a stalled write must not
    wedge the caller (a web executor thread). Instead of joining the outgoing
    thread, ``start`` bumps a generation counter so a superseded thread skips
    its coil-release (the new thread now owns the coils) and exits on its own.

    ``write`` must be a callable taking a :class:`Command`; it is shared with
    the loop/first-frame writers, which serialise on the link's own lock.
    """

    # How long teardown waits for a stopping thread's coil-release to flush
    # before giving up (a class attribute so tests can shorten it).
    JOIN_TIMEOUT_S = 1.0

    def __init__(self, write, max_steps: int = JOG_MAX_STEPS):
        self._write = write
        self._max_steps = max_steps
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Bumped on every start; the running thread releases the coils only
        # while its captured generation is still current (see _run's finally).
        self._generation = 0

    def start(self, direction: int, interval_us: object) -> None:
        interval_s = _clamp_jog_interval_us(interval_us) / 1_000_000
        with self._lock:
            # Supersede any running jog: a higher generation makes the outgoing
            # thread suppress its release, and signalling its stop event makes
            # it exit promptly. No join — the generation guard keeps a lingering
            # (e.g. write-stalled) thread from clobbering the new direction.
            self._generation += 1
            generation = self._generation
            self._stop.set()
            self._stop = stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(direction, interval_s, stop, generation),
                name="flywheel-jog",
                daemon=True,
            )
            self._thread.start()

    def stop(self, join: bool = False) -> bool:
        """Stop the running jog, releasing the coils.

        The generation is left unchanged, so the outgoing thread releases the
        coils in its finally. ``join=True`` (teardown) waits for that release to
        flush before the caller closes the serial port.

        Returns True once the clock is fully stopped (nothing was running, or
        the thread was joined). Returns False only when ``join=True`` and the
        thread did not exit within ``JOIN_TIMEOUT_S`` (e.g. wedged on a serial
        write) — the caller should then release the coils itself before closing.
        """
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            self._stop.set()
            self._thread = None
        if not join:
            return True
        thread.join(timeout=self.JOIN_TIMEOUT_S)
        return not thread.is_alive()

    def _run(
        self, direction: int, interval_s: float, stop: threading.Event, generation: int
    ) -> None:
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
                        "Flywheel jog: hit %d-step safety cap; stopping",
                        self._max_steps,
                    )
        finally:
            # Release the coils only if a newer jog has not superseded us, so a
            # restart's pulses are not clobbered by this thread's stray release.
            # An atomic int read — no lock, so no deadlock with a joining caller.
            if generation == self._generation:
                self._write(release)


@register("flywheel")
def _build(options: dict) -> FlywheelPlugin:
    if serial is None:
        raise RuntimeError("pyserial not installed: pip install pyserial")
    device = str(options.get("device", DEFAULT_DEVICE))
    try:
        baud = int(options.get("baud", DEFAULT_BAUD))
    except (TypeError, ValueError):
        log.warning(
            "Flywheel plugin: invalid baud %r; using %d",
            options.get("baud"),
            DEFAULT_BAUD,
        )
        baud = DEFAULT_BAUD
    return FlywheelPlugin(device=device, baud=baud)


class FlywheelPlugin(Plugin):
    name = "flywheel"

    def __init__(self, device: str = DEFAULT_DEVICE, baud: int = DEFAULT_BAUD):
        self.device = device
        self.baud = baud
        self._link = SerialLink()
        # Bound method (not self._link.write_command) so the clock always
        # writes through the current link, even after a reconnect swaps it.
        self._jog = JogClock(self._write)
        # The jog is one shared motor but the rig is multi-client, so the jog
        # is scoped to the connection that started it: only its owner (or that
        # owner disconnecting) may stop it. Guards owner + clock transitions.
        self._jog_lock = threading.Lock()
        self._jog_owner: int | None = None
        self._closing = False  # set in teardown to refuse jogs racing shutdown

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
            log.warning("Flywheel plugin: failed to open %s: %s", self.device, e)
            return str(e)
        log.info("Flywheel plugin: opened %s @ %d", self.device, self.baud)
        return None

    def teardown(self) -> None:
        with self._jog_lock:
            # Refuse any jog start that races shutdown: once closing, a start
            # that slipped past _jog.stop() below would spawn a thread nothing
            # ever stops. _start_jog checks this flag under the same lock.
            self._closing = True
            self._jog_owner = None
        # Stop unconditionally and wait for the coil-release to flush before the
        # port closes (a released write no-ops once closed). If the thread is
        # wedged past the join timeout, release the coils here ourselves.
        if not self._jog.stop(join=True):
            self._write(Command(n_steps=0))
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
            log.warning("Flywheel plugin: ignoring invalid command %r", spec)
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
            the Flywheel tab from its "serial unavailable" notice to usable.
            """
            error = self._open()
            return {"ready": self._link.is_open, "device": self.device, "error": error}

        @router.post("/api/serial/command")
        def serial_command(payload: dict = Body(...)):
            if not self._link.is_open:
                raise HTTPException(503, "Serial port not available")
            try:
                command = Command.from_payload(payload)
                command.to_bytes()  # range-check the packed wire fields up front
            except (KeyError, TypeError, ValueError, struct.error):
                # struct.error (out-of-range field) is not a ValueError, so
                # without it an out-of-range value would escape as a 500.
                raise HTTPException(422, "Invalid stepper command") from None
            self._link.write_command(command)
            return {"status": "ok"}

        return router

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """Handle hold-to-jog start/stop messages.

        ``{"type": "jog", "action": "start", "direction": -1|1,
        "interval_us": N}`` starts the backend pulse clock in that direction;
        ``{"type": "jog", "action": "stop"}`` stops it. The clock — not these
        messages — paces the steps, so the client sends exactly one of each per
        hold. The jog is scoped to ``client_id`` so concurrent operators don't
        cancel each other (only the owner may stop it).
        """
        if message.get("type") != "jog":
            return False
        if message.get("action") == "start":
            self._start_jog(message, client_id)
        else:  # "stop" (or any non-start jog message) halts the clock
            self._stop_jog(client_id)
        return True

    def on_ws_disconnect(self, client_id: int) -> None:
        # A dropped control socket must not leave the motor spinning, but only
        # if this client owned the jog — another operator's hold is untouched.
        self._stop_jog(client_id)

    def _start_jog(self, message: dict, client_id: int) -> None:
        direction = message.get("direction")
        if direction not in (-1, 1) or not self._link.is_open:
            return  # nothing to drive (covered client-side by the ready gate)
        with self._jog_lock:
            if self._closing:
                return  # shutting down — don't spawn a jog nothing will stop
            # Latest press owns the motor; a previous owner's later release is
            # then ignored (it is no longer the owner) and its hold's stray
            # pulses are superseded by the clock's generation guard.
            self._jog_owner = client_id
            self._jog.start(direction, message.get("interval_us"))

    def _stop_jog(self, client_id: int) -> None:
        with self._jog_lock:
            if client_id != self._jog_owner:
                return  # not the owner — leave the active jog (if any) running
            self._jog_owner = None
            self._jog.stop()
