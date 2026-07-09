"""Flywheel stepper-motor controller plugin (opt-in).

Drives the Arduino stepper over a serial link. Enable it with a ``[[plugins]]``
entry in ``octacam_config.toml`` (settings go under a ``[plugins.options]``
sub-table)::

    [[plugins]]
    name = "flywheel"

    [plugins.options]
    device = "/dev/ttyACM0"
    baud = 115200
    fqbn = "arduino:avr:uno"  # stepper board for firmware flashing (Nano: arduino:avr:nano)
    auto_flash = false        # headless: reflash a stale board without prompting

or with ``octacam gui --plugin flywheel``. Its serial dependency (pyserial)
ships with octacam by default, so no extra install is needed.

**Firmware provisioning.** The firmware reports a banner ``"FLYWHEEL <ver> <build>"``
in reply to a *backward-compatible identify sentinel* (an 8-byte command with
``n_steps=0`` and a marker in ``step_interval_us`` — old firmware just releases the
coils and stays silent, so the command wire format is unchanged and no reflash is
forced). octacam compares ``<build>`` to the ``arduino/stepper_motor`` source and
offers to compile + upload (arduino-cli, ``fqbn`` above) from the GUI's *Flash
firmware* button or ``octacam flash``. See :mod:`octacam.firmware`.

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
from pathlib import Path

from octacam import firmware as fw
from octacam import serial_ports
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

# Firmware identity + provisioning (see octacam.firmware). The stepper protocol is
# frameless (raw 8-byte Command structs), so identify is a *sentinel command*:
# n_steps == 0 (a harmless coil release on any firmware) with step_interval_us set
# to this marker. Firmware that understands it replies "FLYWHEEL <ver> <build>\n";
# older firmware just releases the coils and stays silent. This keeps the command
# wire format unchanged — no reflash is forced, and a board that doesn't answer is
# reported as "no identity" so octacam can offer a (backward-compatible) reflash.
_IDENTIFY_MARKER = 0xFFFF
_EXPECTED_BANNER = "FLYWHEEL"
_DEFAULT_FQBN = "arduino:avr:uno"  # override with the `fqbn` option for a Nano etc.
_PROTOCOL_VERSION = 1


def _firmware_spec(fqbn: str) -> fw.FirmwareSpec | None:
    """The stepper firmware spec for :mod:`octacam.firmware`, or None when the
    sketch source can't be located (a wheel install without a checkout)."""
    sketch = fw.resolve_sketch_dir("stepper_motor")
    if sketch is None:
        return None
    return fw.FirmwareSpec(
        name="flywheel",
        sketch_dir=sketch,
        fqbn=fqbn,
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="FLYWHEEL_FW_BUILD",
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
            raise RuntimeError(
                "pyserial is not importable (it ships with octacam by default, "
                "so the environment may be broken); reinstall with: "
                "pip install pyserial"
            )
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

    def identify(self, banner_prefix: str, timeout: float = 0.5) -> str | None:
        """Send the identify sentinel and read the banner line it triggers.

        Synchronous (there is no background reader): sends the sentinel command,
        then reads a newline-terminated banner. Returns None on a silent board
        (older firmware) or any error. Holds the write lock briefly; only called
        at open, before any jog/loop, so it never contends with motion."""
        sentinel = Command(n_steps=0, step_interval_us=_IDENTIFY_MARKER)
        with self._lock:
            s = self._serial
            if s is None or not s.is_open:
                return None
            try:
                try:
                    s.reset_input_buffer()
                except Exception:
                    pass
                s.write(sentinel.to_bytes())
                s.flush()
                deadline = time.monotonic() + timeout
                buf = bytearray()
                while time.monotonic() < deadline:
                    b = s.read(1)
                    if not b:
                        if buf:
                            break
                        continue
                    if b[0] == 0x0A:  # newline terminates the banner
                        break
                    buf.append(b[0])
                    if len(buf) > 64:
                        break
                line = buf.decode("ascii", "replace").strip()
                return line if line.upper().startswith(banner_prefix.upper()) else None
            except Exception:
                return None


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
        raise RuntimeError(
            "pyserial is not importable (it ships with octacam by default, so "
            "the environment may be broken); reinstall with: pip install pyserial"
        )
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
    fqbn = str(options.get("fqbn", _DEFAULT_FQBN)) or _DEFAULT_FQBN
    auto_flash = options.get("auto_flash", False)
    if isinstance(auto_flash, str):
        auto_flash = auto_flash.strip().lower() in ("1", "true", "yes", "on")
    return FlywheelPlugin(device=device, baud=baud, fqbn=fqbn, auto_flash=bool(auto_flash))


class FlywheelPlugin(Plugin):
    name = "flywheel"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        fqbn: str = _DEFAULT_FQBN,
        auto_flash: bool = False,
    ):
        # _configured_device is what the config asked for (a path, or "auto");
        # self.device is the currently-active/display device, resolved on open.
        self._configured_device = device
        self.device = device
        self.baud = baud
        self._auto_flash = bool(auto_flash)
        self._firmware: str | None = None
        self._firmware_ok = True
        self._last_error: str | None = None
        self._link = SerialLink()
        # Firmware detection + flash lifecycle (shared with the other serial
        # plugins). Owns the port lock; _open takes it too. See octacam.firmware.
        self._fw = fw.FirmwareProvisioner(
            _firmware_spec(fqbn),
            resolve_device=lambda: serial_ports.resolve_device(self._configured_device, self.baud),
            reopen=self._open,
            close_link=lambda: self._link.close(),
            wait_for_device=serial_ports.wait_for_device,
            is_busy=self._fw_is_busy,
        )
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

    def _fw_is_busy(self) -> tuple[bool, str]:
        """Refuse to flash while the motor is jogging (a reset mid-jog would drop
        the coil state)."""
        if getattr(self, "_jog_owner", None) is not None:
            return True, "refusing to flash while the motor is jogging — release it first"
        return False, ""

    # ---------------------------------------------------- process lifecycle

    def setup(self) -> None:
        self._open()

    def _open(self) -> str | None:
        """(Re)open the serial link, returning an error message on failure (else
        None). Never raises: a missing board must not stop the GUI launching,
        and it can be retried at runtime via the reconnect endpoint once the
        board is plugged in.

        Resolves ``device="auto"`` to a single detected board, reads the firmware
        identity, and enriches an open failure with the detected candidate ports.
        Held under the provisioner's port lock so a concurrent flash can't fight
        over the port (re-entrant: flash's reopen calls this on the same thread)."""
        with self._fw.port_lock:
            self._firmware = None
            self._firmware_ok = True
            self._last_error = None
            device, reason = serial_ports.resolve_device(self._configured_device, self.baud)
            if device is None:
                log.warning("Flywheel plugin: %s", reason)
                return reason
            if device != self.device:
                log.info("Flywheel plugin: %s", reason)
                self.device = device
            try:
                self._link.open(device, self.baud)
            except Exception as e:
                msg = serial_ports.explain_open_failure(device, e)
                log.warning("Flywheel plugin: %s", msg)
                return msg
            log.info("Flywheel plugin: opened %s @ %d", device, self.baud)
            self._verify_identity()
            return None

    def _verify_identity(self) -> None:
        """Read the firmware banner (via the identify sentinel) and classify it.

        The stepper command protocol is unchanged, so an OUTDATED or UNIDENTIFIED
        board still accepts commands; only a foreign banner or wrong version
        disables driving and offers a reflash."""
        banner = self._link.identify(_EXPECTED_BANNER)
        self._firmware = banner
        check = self._fw.classify(banner)
        if check is None:
            if banner:
                name, version, _ = fw.parse_banner(banner)
                self._firmware_ok = name == _EXPECTED_BANNER.upper() and (
                    version is None or version == _PROTOCOL_VERSION
                )
            else:
                self._firmware_ok = True
            return
        self._firmware_ok = fw.arm_compatible(check)
        if check.needs_flash and check.state is not fw.FirmwareState.OUTDATED:
            log.warning("Flywheel plugin: %s — %s; run `octacam flash` to update.",
                        self.device, check.detail)

    def firmware_provisioning(self) -> dict:
        """Firmware picture for `octacam flash` and the GUI."""
        return self._fw.provisioning(
            plugin_name=self.name,
            device=self.device,
            firmware=self._firmware,
            firmware_ok=self._firmware_ok,
            extra={"auto_flash": self._auto_flash},
        )

    def flash_firmware(self, on_line=None) -> fw.FlashResult:
        """Compile + upload the current stepper firmware. Delegates the
        close→upload→reopen→re-verify lifecycle to the shared provisioner. Never
        raises."""
        result = self._fw.flash(on_line=on_line)
        if not result.ok:
            self._last_error = result.message
        return result

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
        check = self._fw.check
        return {
            "device": self.device,
            "firmware": self._firmware,
            "firmware_ok": self._firmware_ok,
            "firmware_state": check.state.value if check else None,
            "needs_flash": bool(check and check.needs_flash),
            "error": self._last_error,
        }

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

    def web_assets(self) -> Path:
        """The plugin's co-located JS/CSS folder, served at /plugins/flywheel/."""
        return Path(__file__).parent / "web"

    def api_router(self):
        from fastapi import APIRouter, Body, HTTPException

        router = APIRouter()

        @router.post("/api/serial/reconnect")
        def serial_reconnect(payload: dict = Body(default={})):
            """Re-attempt opening the serial port.

            Lets the operator recover from a board that was unplugged or absent
            at launch (and is now connected) without restarting the server. An
            optional ``{"device": "/dev/…"}`` body switches to a different port
            (e.g. picked from the GUI dropdown) before reopening. The response
            carries the resulting ``ready`` state so the GUI can flip the
            Flywheel tab from its "serial unavailable" notice to usable.
            """
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self._configured_device = device.strip()
            error = self._open()
            check = self._fw.check
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "error": error,
                "firmware": self._firmware,
                "firmware_ok": self._firmware_ok,
                "firmware_state": check.state.value if check else None,
                "needs_flash": bool(check and check.needs_flash),
            }

        @router.get("/api/flywheel/firmware")
        def get_firmware():
            """Firmware state vs. the sketch source + whether octacam can flash it."""
            return self.firmware_provisioning()

        @router.post("/api/flywheel/flash")
        def flash(payload: dict = Body(default={})):
            """Compile + upload the current firmware, then report the new state."""
            result = self.flash_firmware()
            return {
                **result.to_dict(),
                "firmware": self._firmware,
                "firmware_ok": self._firmware_ok,
                "ready": self._link.is_open,
                "provisioning": self.firmware_provisioning(),
            }

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
