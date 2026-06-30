"""2-photon rig hardware trigger plugin (opt-in).

Arms an Arduino-based camera trigger over a serial link. The Arduino waits for
a ThorSync rising edge, then generates a precise square-wave camera trigger at
the configured frame rate for the configured duration. Enable it with a
``[[plugins]]`` entry in ``octacam_config.toml`` (settings go under a
``[plugins.options]`` sub-table)::

    [[plugins]]
    name = "twophoton"

    [plugins.options]
    device = "/dev/arduinoCams"  # udev symlink or /dev/ttyACM0, COM3, etc.
    baud = 115200                # optional; default 115200
    default_fps = 100            # fallback when GUI params are not sent
    default_duration_ms = 10000  # fallback duration in milliseconds

The plugin can also be enabled at launch time with ``--plugin twophoton``.
Its serial dependency (pyserial) ships with octacam by default, so no extra
install is needed.

Wire protocol (host → Arduino, 7 bytes little-endian):
  [0xA5][fps:uint16][duration_ms:uint32]  — arm
  [0xCA]                                  — cancel / abort

Wire protocol (Arduino → host, 1 byte):
  'A' — armed, waiting for ThorSync
  'T' — triggered, capture running
  'D' — done, capture complete

The plugin broadcasts Arduino state changes over the GUI WebSocket so the
operator sees real-time feedback without polling.

To package this plugin independently (e.g. as ``octacam-twophoton``):
  1. Move this file to the new package's ``octacam_twophoton/plugin.py``.
  2. Remove it from ``octacam.plugins._BUILTINS``.
  3. Register the factory via::
       [project.entry-points."octacam.plugins"]
       twophoton = "octacam_twophoton.plugin:_build"
  The entry-point discovery in ``octacam.plugins`` will pick it up automatically.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass

from octacam.plugins import register
from octacam.plugins.base import Plugin

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/arduinoCams"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 100
DEFAULT_DURATION_MS = 10_000

# How long on_recording_start waits for the firmware's 'A' acknowledgement
# before warning that the arm may not have taken. The firmware acks within a
# few ms; the wait runs off the controller lock, so it only delays the start
# response, never telemetry.
ACK_TIMEOUT_S = 1.0

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# Wire-format constants
_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
# magic (uint8) + fps (uint16 LE) + duration_ms (uint32 LE) = 7 bytes
_ARM_FORMAT = "<BHI"

_STATUS_BYTES = frozenset(b"ATD")


@dataclass
class ArmParams:
    fps: int
    duration_ms: int

    def to_bytes(self) -> bytes:
        return struct.pack(_ARM_FORMAT, _ARM_MAGIC, self.fps, self.duration_ms)

    @classmethod
    def from_payload(
        cls, payload: dict, default_fps: int, default_duration_ms: int
    ) -> ArmParams:
        """Build from a plugin_params dict; falls back to defaults on missing keys."""
        try:
            fps = int(payload.get("fps", default_fps))
        except (TypeError, ValueError):
            fps = default_fps
        try:
            duration_ms = int(payload.get("duration_ms", default_duration_ms))
        except (TypeError, ValueError):
            duration_ms = default_duration_ms
        fps = max(1, min(10_000, fps))
        # Clamp to the uint32 wire field, mirroring the fps clamp. Without an
        # upper bound an absurd duration makes struct.pack raise inside send_arm,
        # which dispatch swallows — silently skipping the arm.
        duration_ms = max(1, min(0xFFFF_FFFF, duration_ms))
        return cls(fps=fps, duration_ms=duration_ms)


class TwoPhotonLink:
    """Serial link to the 2-photon trigger Arduino.

    Writes arm/cancel packets to the Arduino and reads back single-byte status
    replies on a dedicated background thread. The status callback is called from
    that thread; callers must be thread-safe.
    """

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_broken: Callable[[], None] | None = None,
    ):
        self._serial = None
        self._write_lock = threading.Lock()
        # Serializes the open/close/reconnect lifecycle so two concurrent
        # reconnects (double-click, two browser tabs, or a reconnect racing
        # teardown) cannot each create a port and leak the loser's FD + reader.
        self._lifecycle_lock = threading.Lock()
        self._on_status = on_status
        # Called from the reader thread when the port dies mid-session (not on a
        # clean close), so the owner can surface the lost link to the GUI.
        self._on_broken = on_broken
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

    def open(self, device: str, baud: int) -> None:
        if serial is None:
            raise RuntimeError(_NO_PYSERIAL_MSG)
        with self._lifecycle_lock:
            self._close_locked()
            s = serial.Serial(device, baud, timeout=0.2, write_timeout=1)
            self._serial = s
            self._reader_stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="twophoton-reader"
            )
            self._reader.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Tear down the port and reader. Caller must hold ``_lifecycle_lock``."""
        self._reader_stop.set()
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                s.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    def _mark_broken(self) -> None:
        """Drop the handle after the port dies under the reader thread.

        Without this the reader exits but pyserial's ``is_open`` stays True, so
        ``is_open``/``is_ready`` would report a dead link as usable forever and
        the GUI would never offer reconnect. Touches only ``_write_lock`` (never
        the lifecycle lock) so it can't deadlock a concurrent close() joining us.
        """
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        # Notify outside the write lock (and never the lifecycle lock) so a
        # broadcast hook can't deadlock a concurrent close() that is joining us.
        if self._on_broken is not None:
            try:
                self._on_broken()
            except Exception:
                log.exception("2-photon trigger: on_broken callback error")

    @property
    def is_open(self) -> bool:
        s = self._serial
        return s is not None and s.is_open

    def _write(self, data: bytes) -> None:
        with self._write_lock:
            s = self._serial
            if s is None or not s.is_open:
                return
            try:
                s.write(data)
            except serial.SerialException as e:  # pyright: ignore[reportOptionalMemberAccess]
                log.warning("2-photon trigger: serial write failed: %s", e)

    def send_arm(self, params: ArmParams) -> None:
        self._write(params.to_bytes())

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def _read_loop(self) -> None:
        while not self._reader_stop.is_set():
            s = self._serial
            if s is None or not s.is_open:
                break
            try:
                b = s.read(1)
            except serial.SerialException:  # pyright: ignore[reportOptionalMemberAccess]
                # Port died under us (e.g. unplugged mid-run). Drop the handle so
                # is_ready() turns False and the GUI surfaces the reconnect path,
                # unless we are already shutting down cleanly.
                if not self._reader_stop.is_set():
                    self._mark_broken()
                break
            except Exception:
                # The port was closed under us (s.fd → None during shutdown),
                # which surfaces as TypeError from os.read(None, 1). Any other
                # unexpected exception should also not crash the daemon thread.
                if not self._reader_stop.is_set():
                    log.debug(
                        "2-photon trigger: read error in reader thread", exc_info=True
                    )
                    self._mark_broken()
                break
            if b and b[0] in _STATUS_BYTES:
                try:
                    self._on_status(chr(b[0]))
                except Exception:
                    log.exception("2-photon trigger: status callback error")


_STATE_LABELS: dict[str, str] = {
    "A": "armed",
    "T": "triggered",
    "D": "done",
}


@register("twophoton")
def _build(options: dict) -> TwoPhotonPlugin:
    if serial is None:
        raise RuntimeError(_NO_PYSERIAL_MSG)
    device = str(options.get("device") or DEFAULT_DEVICE)
    try:
        baud = int(options.get("baud", DEFAULT_BAUD))
    except (TypeError, ValueError):
        log.warning(
            "twophoton plugin: invalid baud %r; using %d",
            options.get("baud"),
            DEFAULT_BAUD,
        )
        baud = DEFAULT_BAUD
    try:
        default_fps = int(options.get("default_fps", DEFAULT_FPS))
    except (TypeError, ValueError):
        default_fps = DEFAULT_FPS
    try:
        default_duration_ms = int(
            options.get("default_duration_ms", DEFAULT_DURATION_MS)
        )
    except (TypeError, ValueError):
        default_duration_ms = DEFAULT_DURATION_MS
    return TwoPhotonPlugin(
        device=str(device),
        baud=baud,
        default_fps=default_fps,
        default_duration_ms=default_duration_ms,
    )


class TwoPhotonPlugin(Plugin):
    """2-photon rig hardware trigger plugin.

    Arms the Arduino with the recording's fps and duration, then waits for the
    ThorSync rising edge to start capture. Arduino state changes are broadcast
    over the GUI WebSocket so the operator sees real-time feedback.
    """

    name = "twophoton"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
    ):
        self.device = device
        self.baud = baud
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._link = TwoPhotonLink(self._on_arduino_status, on_broken=self._on_link_broken)
        self._arduino_state = "idle"
        # Set by the status reader when the firmware acknowledges an arm ('A').
        # on_recording_start waits on it so a silently-dropped arm is surfaced
        # instead of leaving the cameras waiting on a trigger that never fires.
        self._armed_event = threading.Event()
        self._ack_timeout_s = ACK_TIMEOUT_S
        # Injected by app.py via set_broadcast() once the web app is created.
        self._broadcast: Callable[[str, dict], None] | None = None

    # -------------------------------------------------- broadcast injection

    def set_broadcast(self, callback: Callable[[str, dict], None]) -> None:
        """Inject the WebSocket broadcast hook (called by app.py at startup)."""
        self._broadcast = callback

    def _on_arduino_status(self, status: str) -> None:
        state = _STATE_LABELS.get(status, "idle")
        if state == "armed":
            self._armed_event.set()  # release a pending on_recording_start ack wait
        self._set_arduino_state(state)

    def _on_link_broken(self) -> None:
        """Reader-thread hook: the serial port died mid-session. Re-broadcast the
        state so the GUI sees ``ready=False`` and disables the arm gate (and shows
        the reconnect notice) instead of carrying a stale ``ready`` that would arm
        a dead link on the next recording."""
        self._set_arduino_state("idle")

    def _set_arduino_state(self, state: str) -> None:
        self._arduino_state = state
        if self._broadcast is not None:
            # Carry link readiness with every state push so a client that
            # connected before the port opened (or after it died) keeps its arm
            # gate in sync without a separate poll.
            self._broadcast(
                "twophoton_state",
                {"state": state, "device": self.device, "ready": self._link.is_open},
            )

    # -------------------------------------------------- process lifecycle

    def setup(self) -> None:
        self._open()

    def _open(self) -> str | None:
        """(Re)open the serial link; returns an error message on failure, else None."""
        try:
            self._link.open(self.device, self.baud)
        except Exception as e:
            log.warning("2-photon trigger: failed to open %s: %s", self.device, e)
            return str(e)
        log.info("2-photon trigger: opened %s @ %d", self.device, self.baud)
        return None

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self._arduino_state = "idle"

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        return {"device": self.device, "arduino_state": self._arduino_state}

    # -------------------------------------------------- recording lifecycle

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the Arduino when the GUI's "Arm with recording" checkbox is checked.

        Only arms when ``params["twophoton"]`` is present — its absence means the
        operator left the checkbox unchecked.  ``fps`` and ``duration_ms`` inside
        that dict are optional; they fall back to the plugin's configured defaults.
        """
        spec = (params or {}).get("twophoton")
        if spec is None:
            return
        arm = ArmParams.from_payload(spec, self._default_fps, self._default_duration_ms)
        if not self._link.is_open:
            # send_arm would silently no-op on a closed link, leaving the cameras
            # waiting on an external trigger that never fires. Surface it instead.
            log.warning(
                "2-photon trigger: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        log.info(
            "2-photon trigger: arming at %d fps for %d ms", arm.fps, arm.duration_ms
        )
        self._armed_event.clear()
        self._link.send_arm(arm)
        # Wait briefly for the firmware's 'A' acknowledgement. A dropped or
        # garbled arm packet (or one whose payload arrives too late for the
        # firmware's parse window) otherwise fails silently and the cameras wait
        # on an external trigger that never fires; warn so the operator knows.
        if self._ack_timeout_s > 0 and not self._armed_event.wait(self._ack_timeout_s):
            log.warning(
                "2-photon trigger: no arm acknowledgement from %s within %.1f s; "
                "the board may not have armed (cameras could wait for a trigger "
                "that never fires)",
                self.device,
                self._ack_timeout_s,
            )

    def on_recording_stop(self, aborted: bool) -> None:
        # Stop the hardware trigger whenever a recording ends — abort, manual
        # early stop, or clean duration-elapsed finish. A manual stop arrives
        # with aborted=False while the firmware may still be RUNNING, so
        # cancelling only on abort would leave the Arduino emitting trigger
        # pulses for its full configured duration after the cameras stopped. A
        # cancel sent to an already-IDLE board (clean completion that already
        # sent 'D') is a harmless no-op. The firmware's cancel path returns to
        # IDLE silently (no status byte), so reset+broadcast our own state too,
        # or the GUI would keep showing 'armed'/'triggered' until the next arm.
        self._link.send_cancel()
        self._set_arduino_state("idle")

    # -------------------------------------------------- web contributions

    def api_router(self):
        from fastapi import APIRouter

        router = APIRouter()

        @router.post("/api/twophoton/reconnect")
        def reconnect():
            """Re-attempt opening the serial port after an unplug/replug."""
            error = self._open()
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "error": error,
                "arduino_state": self._arduino_state,
            }

        @router.get("/api/twophoton/status")
        def get_status():
            """Current connection and Arduino state (for initial page load)."""
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "arduino_state": self._arduino_state,
            }

        return router
