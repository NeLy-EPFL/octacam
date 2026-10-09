"""Flywheel stepper-motor controller plugin (opt-in).

::

    [[plugins]]
    name = "flywheel"

    [plugins.options]
    device = "/dev/ttyACM0"
    baud = 115200
    fqbn = "arduino:avr:uno"  # the board, for flashing (a Nano: arduino:avr:nano)
    auto_flash = false        # headless: reflash a stale board without asking

It fires the armed loop command at the recording's first frame, runs one on
demand (`POST /api/serial/command`), and steps the motor while a GUI jog
button is held.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from octacam import firmware as fw
from octacam.plugins.serial import SerialLink, SerialPlugin

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 115200

# A jog writes one half-step command per tick, so its interval is bounded by an
# 8-byte write at the baud rate (~0.7 ms at 115200); 65535 us is the field's max.
JOG_MIN_INTERVAL_US = 1000
JOG_MAX_INTERVAL_US = 65535
JOG_DEFAULT_INTERVAL_US = 2000
# Ends a jog whose release never arrives (a lost message, a frozen tab): ~24
# turns of a 4096-half-step motor.
JOG_MAX_STEPS = 100_000

# arduino/stepper_motor's packed Command struct.
_COMMAND_FORMAT = "<hHHBB"
COMMAND_FIELDS = (
    "n_steps",
    "step_interval_us",
    "rest_duration_ms",
    "n_repeats",
    "init_wait_duration_s",
)

# The protocol is bare 8-byte commands, so identify is a sentinel: n_steps = 0
# (a coil release on any firmware) with this marker as the interval. Current
# firmware answers "FLYWHEEL <version> <build>"; older firmware just releases the
# coils, so the wire format is unchanged and no reflash is forced.
_IDENTIFY_MARKER = 0xFFFF
_EXPECTED_BANNER = "FLYWHEEL"
_DEFAULT_FQBN = "arduino:avr:uno"  # the `fqbn` option overrides it
_PROTOCOL_VERSION = 1


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
    def parse(cls, payload) -> Command | None:
        """The Command a dict of its fields describes, or None when a field is
        missing, not an integer, or outside its wire range.
        """
        try:
            command = cls(**{field: int(payload[field]) for field in COMMAND_FIELDS})
            command.to_bytes()  # struct.error for an out-of-range field
        except KeyError, TypeError, ValueError, struct.error:
            return None
        return command


def _command_from_options(options: dict) -> Command | None:
    """The rig's loop command (`options.command`), or None. A missing field
    keeps its default; an invalid table is warned about and ignored.
    """
    raw = options.get("command")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning("Flywheel plugin: 'command' must be a table; ignoring %r", raw)
        return None
    command = Command.parse({**asdict(Command()), **raw})
    if command is None:
        log.warning("Flywheel plugin: ignoring invalid command %r", raw)
    return command


class FlywheelLink(SerialLink):
    """The serial link to the stepper. Commands come from the monitor thread
    (first frame) and web threads (jog); the base serializes the writes. Its
    only reply is the banner line the identify sentinel triggers.
    """

    name = "flywheel"
    banner_prefix = _EXPECTED_BANNER
    identify_query = Command(n_steps=0, step_interval_us=_IDENTIFY_MARKER).to_bytes()

    def write_command(self, command: Command) -> None:
        self._write(command.to_bytes())

    def _feed(self, chunk: bytes) -> None:
        for line in self._lines(chunk):
            self._identified(line)


def _clamp_jog_interval_us(value) -> int:
    """A jog interval clamped to the supported us range; the default for a
    missing or non-numeric one.
    """
    try:
        us = int(value)
    except TypeError, ValueError:
        return JOG_DEFAULT_INTERVAL_US
    return max(JOG_MIN_INTERVAL_US, min(JOG_MAX_INTERVAL_US, us))


class JogClock:
    """Steps the motor at a fixed interval while a jog button is held.

    A thread writes one half-step command per tick, so the step rate comes from
    a clock, not from the (jittery) WebSocket messages; stopping releases the
    coils (`n_steps = 0`). `start` and `stop` never block on the serial
    link: a new start bumps a generation counter instead of joining the old
    thread, which then skips its coil release (the new thread owns the coils).
    """

    # How long teardown waits for a stopping jog's coil release to flush.
    JOIN_TIMEOUT_S = 1.0

    def __init__(self, write, max_steps: int = JOG_MAX_STEPS):
        self._write = write
        self._max_steps = max_steps
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0

    def start(self, direction: int, interval_us: object) -> None:
        interval_s = _clamp_jog_interval_us(interval_us) / 1_000_000
        with self._lock:
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
        """Stop the jog; its thread releases the coils. `join=True` (teardown)
        waits for that release and returns False when the thread did not exit
        in time (a wedged write): the caller must then release the coils.
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
                else:  # behind (a saturated link): re-anchor, no backlog
                    next_tick = now
            else:
                if steps >= self._max_steps:
                    log.warning(
                        "Flywheel jog: hit %d-step safety cap; stopping",
                        self._max_steps,
                    )
        finally:
            # Release the coils unless a newer jog took them over: under the lock
            # start() bumps the generation with, so check and release are atomic.
            # No caller joins a jog thread while holding it.
            with self._lock:
                if generation == self._generation:
                    self._write(release)


class FlywheelPlugin(SerialPlugin[FlywheelLink]):
    name = "flywheel"
    web_dir = Path(__file__).parent / "web"
    firmware = fw.FirmwareSpec(
        name="flywheel",
        sketch_dir=fw.resolve_sketch_dir("stepper_motor"),
        fqbn=_DEFAULT_FQBN,
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="FLYWHEEL_FW_BUILD",
    )
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/serial/reconnect"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        fqbn: str = _DEFAULT_FQBN,
        auto_flash: bool = False,
        command: Command | None = None,
    ):
        super().__init__(
            FlywheelLink(self._on_link_broken),
            device=device,
            baud=baud,
            auto_flash=bool(auto_flash),
            firmware=replace(self.firmware, fqbn=fqbn),
        )
        # The configured loop; the tab edits its own copy, so this stays the config's.
        self._command = command
        self._jog = JogClock(self._write)
        # One motor, many clients: a jog belongs to the connection that started
        # it, and only that one (or its disconnect) stops it.
        self._jog_lock = threading.Lock()
        self._jog_owner: int | None = None
        self._closing = False  # set in teardown to refuse jogs racing shutdown

    @classmethod
    def from_options(cls, options: dict) -> FlywheelPlugin:
        device = str(options.get("device", DEFAULT_DEVICE))
        try:
            baud = int(options.get("baud", DEFAULT_BAUD))
        except TypeError, ValueError:
            log.warning(
                "Flywheel plugin: invalid baud %r; using %d",
                options.get("baud"),
                DEFAULT_BAUD,
            )
            baud = DEFAULT_BAUD
        fqbn = str(options.get("fqbn", _DEFAULT_FQBN)) or _DEFAULT_FQBN
        auto_flash = options.get("auto_flash", False)
        if isinstance(auto_flash, str):
            auto_flash = auto_flash.strip().lower() in ("1", "true", "yes", "on")
        return cls(
            device=device,
            baud=baud,
            fqbn=fqbn,
            auto_flash=bool(auto_flash),
            command=_command_from_options(options),
        )

    def _write(self, command: Command) -> None:
        self._link.write_command(command)

    def busy_reason(self) -> str | None:
        # The board's reset would drop the coil state.
        if self._jog_owner is not None:
            return (
                "refusing to flash while the motor is jogging \N{EM DASH} release it "
                "first"
            )
        return None

    def _on_link_broken(self) -> None:
        pass  # is_ready() reads the dead link; the tab has no state to push

    def teardown(self) -> None:
        with self._jog_lock:
            # A jog started after the stop below would run forever.
            self._closing = True
            self._jog_owner = None
        # The coil release must flush before the port closes; a wedged jog
        # thread leaves it to us.
        if not self._jog.stop(join=True):
            self._write(Command(n_steps=0))
        self._link.close()

    def status(self) -> dict:
        status = super().status()
        if self._command is not None:
            # Seeds the tab's loop fields with the configured program.
            status["command"] = asdict(self._command)
        return status

    # -------------------------------------------------- recording lifecycle

    def snapshot_options(self, params: dict | None) -> dict | None:
        """The loop command a recording armed, as config options; None when it
        armed none or the configured one.
        """
        command = self._command_from(params)
        if command is None or command == self._command:
            return None
        return {"command": asdict(command)}

    def default_start_params(self, fps: float, duration_s: float) -> dict | None:
        """The configured loop for headless `octacam record` (and a relaunch
        from a recording's snapshot); None without one, so the CLI never spins an
        unconfigured motor.
        """
        if self._command is None:
            return None
        return asdict(self._command)

    def on_first_frame(self, params: dict | None) -> None:
        command = self._command_from(params)
        if command is not None:
            self._link.write_command(command)

    def _command_from(self, params: dict | None) -> Command | None:
        if not params:
            return None
        command = Command.parse(params)
        if command is None:
            log.warning("Flywheel plugin: ignoring invalid command %r", params)
        return command

    # --------------------------------------------------------- web contrib

    def api_router(self):
        from fastapi import Body, HTTPException

        router = super().api_router()

        @router.post("/api/serial/command")
        def serial_command(payload: dict = Body(...)):
            if not self._link.is_open:
                raise HTTPException(503, "Serial port not available")
            command = Command.parse(payload)
            if command is None:
                raise HTTPException(422, "Invalid stepper command")
            self._link.write_command(command)
            return {"status": "ok"}

        return router

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """Hold-to-jog: `{"type": "jog", "action": "start", "direction": -1|1,
        "interval_us": N}` starts the clock and any other jog action stops it,
        one of each per hold. Only the client that started a jog stops it.
        """
        if message.get("type") != "jog":
            return False
        if message.get("action") == "start":
            self._start_jog(message, client_id)
        else:
            self._stop_jog(client_id)
        return True

    def on_ws_disconnect(self, client_id: int) -> None:
        self._stop_jog(client_id)

    def _start_jog(self, message: dict, client_id: int) -> None:
        direction = message.get("direction")
        if direction not in (-1, 1) or not self._link.is_open:
            return
        with self._jog_lock:
            if self._closing:
                return
            # The latest press owns the motor; an earlier owner's release is
            # then ignored.
            self._jog_owner = client_id
            self._jog.start(direction, message.get("interval_us"))

    def _stop_jog(self, client_id: int) -> None:
        with self._jog_lock:
            if client_id != self._jog_owner:
                return
            self._jog_owner = None
            self._jog.stop()
