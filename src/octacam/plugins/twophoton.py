"""2-photon rig hardware trigger plugin (opt-in).

Arms an Arduino-based camera trigger over a serial link. The Arduino waits for
a ThorSync rising edge, then generates a precise square-wave camera trigger at
the configured frame rate for the configured duration. Enable it with a
``[[plugins]]`` entry in ``octacam_config.toml``::

    [[plugins]]
    name = "twophoton"
    device = "/dev/ArduinoCam"   # udev symlink or /dev/ttyACM0, COM3, etc.
    baud = 115200                # optional; default 115200
    default_fps = 100            # fallback when GUI params are not sent
    default_duration_ms = 10000  # fallback duration in milliseconds

The plugin can also be enabled at launch time with ``--plugin twophoton``.
Requires the optional dependency: ``pip install octacam[twophoton]``.

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

DEFAULT_BAUD = 115200
DEFAULT_FPS = 100
DEFAULT_DURATION_MS = 10_000

# Wire-format constants
_ARM_MAGIC    = 0xA5
_CANCEL_MAGIC = 0xCA
# magic (uint8) + fps (uint16 LE) + duration_ms (uint32 LE) = 7 bytes
_ARM_FORMAT   = "<BHI"

_STATUS_BYTES = frozenset(b"ATD")


@dataclass
class ArmParams:
    fps: int
    duration_ms: int

    def to_bytes(self) -> bytes:
        return struct.pack(_ARM_FORMAT, _ARM_MAGIC, self.fps, self.duration_ms)

    @classmethod
    def from_payload(cls, payload: dict, default_fps: int, default_duration_ms: int) -> "ArmParams":
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
        duration_ms = max(1, duration_ms)
        return cls(fps=fps, duration_ms=duration_ms)


class TwoPhotonLink:
    """Serial link to the 2-photon trigger Arduino.

    Writes arm/cancel packets to the Arduino and reads back single-byte status
    replies on a dedicated background thread. The status callback is called from
    that thread; callers must be thread-safe.
    """

    def __init__(self, on_status: Callable[[str], None]):
        self._serial = None
        self._write_lock = threading.Lock()
        self._on_status = on_status
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

    def open(self, device: str, baud: int) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial not installed: pip install octacam[twophoton]"
            )
        self.close()
        s = serial.Serial(device, baud, timeout=0.2, write_timeout=1)
        self._serial = s
        self._reader_stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="twophoton-reader"
        )
        self._reader.start()

    def close(self) -> None:
        self._reader_stop.set()
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                s.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

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
                break
            if b and b[0:1] in (bytes([x]) for x in _STATUS_BYTES):
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
def _build(options: dict) -> "TwoPhotonPlugin":
    if serial is None:
        raise RuntimeError(
            "pyserial not installed: pip install octacam[twophoton]"
        )
    device = options.get("device")
    if not device:
        raise RuntimeError(
            "twophoton plugin: 'device' is required in the plugin config "
            "(e.g. device = \"/dev/ArduinoCam\")"
        )
    try:
        baud = int(options.get("baud", DEFAULT_BAUD))
    except (TypeError, ValueError):
        log.warning("twophoton plugin: invalid baud %r; using %d", options.get("baud"), DEFAULT_BAUD)
        baud = DEFAULT_BAUD
    try:
        default_fps = int(options.get("default_fps", DEFAULT_FPS))
    except (TypeError, ValueError):
        default_fps = DEFAULT_FPS
    try:
        default_duration_ms = int(options.get("default_duration_ms", DEFAULT_DURATION_MS))
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
        device: str,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
    ):
        self.device = device
        self.baud = baud
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._link = TwoPhotonLink(self._on_arduino_status)
        self._arduino_state = "idle"
        # Injected by app.py via set_broadcast() once the web app is created.
        self._broadcast: Callable[[str, dict], None] | None = None

    # -------------------------------------------------- broadcast injection

    def set_broadcast(self, callback: Callable[[str, dict], None]) -> None:
        """Inject the WebSocket broadcast hook (called by app.py at startup)."""
        self._broadcast = callback

    def _on_arduino_status(self, status: str) -> None:
        self._arduino_state = _STATE_LABELS.get(status, "idle")
        if self._broadcast is not None:
            self._broadcast(
                "twophoton_state",
                {"state": self._arduino_state, "device": self.device},
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

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        return {"device": self.device, "arduino_state": self._arduino_state}

    # -------------------------------------------------- recording lifecycle

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the Arduino with the recording's fps and duration.

        ``params["twophoton"]`` may carry ``fps`` and ``duration_ms`` sent by
        the GUI; falls back to the plugin's configured defaults when absent.
        """
        spec = (params or {}).get("twophoton", {})
        arm = ArmParams.from_payload(spec, self._default_fps, self._default_duration_ms)
        log.info(
            "2-photon trigger: arming at %d fps for %d ms", arm.fps, arm.duration_ms
        )
        self._link.send_arm(arm)

    def on_recording_stop(self, aborted: bool) -> None:
        if aborted:
            self._link.send_cancel()

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
