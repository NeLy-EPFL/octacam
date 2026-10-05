"""2-photon rig hardware trigger plugin (opt-in).

An Arduino that waits for ThorSync's edge, then triggers the cameras at the
recording's fps for its duration::

    [[plugins]]
    name = "twophoton"

    [plugins.options]
    device = "/dev/arduinoCams"  # a udev symlink, /dev/ttyACM0, COM3 or "auto"
    baud = 115200
    default_fps = 100            # for a start slice without one
    default_duration_ms = 10000
    auto_flash = false           # headless: reflash a stale board without asking

Wire protocol, host -> Arduino (little-endian):
  [0xA5][fps u16][duration_ms u32]  arm
  [0xCA]                            cancel
  [0x3F] '?'                        identify: "2PHOTON <version> <build>" + newline
Arduino -> host, one byte: 'A' armed (waiting for ThorSync), 'T' triggered,
'D' done. The banner's build is checked and flashed via :mod:`octacam.firmware`.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from octacam import firmware as fw
from octacam.plugins.serial import SerialLink, SerialPlugin

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/arduinoCams"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 100
DEFAULT_DURATION_MS = 10_000

# How long an arm waits for the board's 'A' (sent within ms) before reporting it.
ACK_TIMEOUT_S = 1.0

_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
_ARM_FORMAT = "<BHI"

_STATE_LABELS = {"A": "armed", "T": "triggered", "D": "done"}

# Starts with '2', never a status byte, so the reader tells the two apart.
_EXPECTED_BANNER = "2PHOTON"
_PROTOCOL_VERSION = 1


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
        """From a start slice, each field falling back to its default."""
        try:
            fps = int(payload.get("fps", default_fps))
        except (TypeError, ValueError):
            fps = default_fps
        try:
            duration_ms = int(payload.get("duration_ms", default_duration_ms))
        except (TypeError, ValueError):
            duration_ms = default_duration_ms
        fps = max(1, min(10_000, fps))
        # Within the wire field: a struct.error in arm() would skip the arm.
        duration_ms = max(1, min(0xFFFF_FFFF, duration_ms))
        return cls(fps=fps, duration_ms=duration_ms)


class TwoPhotonLink(SerialLink):
    """The serial link to the 2-photon trigger: bare status bytes, plus the
    newline-terminated banner in reply to identify."""

    name = "twophoton"
    banner_prefix = _EXPECTED_BANNER

    def __init__(self, on_status: Callable[[str], None], on_broken: Callable[[], None]):
        super().__init__(on_broken)
        self._on_status = on_status
        self._armed = threading.Event()  # set by the board's 'A'

    def arm(self, params: ArmParams) -> str:
        """Send an arm: ``"ok"`` ('A' within ACK_TIMEOUT_S), ``"timeout"`` or
        ``"write_failed"``."""
        self._armed.clear()
        if not self._write(params.to_bytes()):
            return "write_failed"
        return "ok" if self._armed.wait(ACK_TIMEOUT_S) else "timeout"

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def _feed(self, chunk: bytes) -> None:
        # A status byte with an empty buffer is a status; anything else builds
        # the banner line.
        for byte in chunk:
            if byte == 0x0A:
                self._identified(self._buf.decode("ascii", "replace").strip())
                self._buf.clear()
            elif self._buf:
                self._buf.append(byte)
                if len(self._buf) > 64:  # a runaway or garbled line
                    self._buf.clear()
            elif chr(byte) in _STATE_LABELS:
                if byte == ord("A"):
                    self._armed.set()
                self._dispatch(self._on_status, chr(byte))
            else:
                self._buf.append(byte)  # the start of a banner line


class TwoPhotonPlugin(SerialPlugin):
    """Arms the 2-photon trigger with the recording's fps and duration."""

    name = "twophoton"
    web_dir = Path(__file__).parent / "web"
    firmware = fw.FirmwareSpec(
        name="twophoton",
        sketch_dir=fw.resolve_sketch_dir("2photon_trigger"),
        fqbn="arduino:avr:mega",  # Arduino Mega 2560
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="TWOPHOTON_FW_BUILD",
    )
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/twophoton/reconnect"
    state_topic = "twophoton_state"
    _link: TwoPhotonLink

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        auto_flash: bool = False,
    ):
        super().__init__(
            TwoPhotonLink(self._on_status, self._on_link_broken),
            device=device,
            baud=baud,
            auto_flash=bool(auto_flash),
        )
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms

    @classmethod
    def from_options(cls, options: dict) -> TwoPhotonPlugin:
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
        auto_flash = options.get("auto_flash", False)
        if isinstance(auto_flash, str):
            auto_flash = auto_flash.strip().lower() in ("1", "true", "yes", "on")
        return cls(
            device=device,
            baud=baud,
            default_fps=default_fps,
            default_duration_ms=default_duration_ms,
            auto_flash=bool(auto_flash),
        )

    def busy_reason(self) -> str | None:
        if self.board_state in ("armed", "triggered"):
            return "refusing to flash while the trigger is armed/running — stop the recording first"
        return None

    def _on_status(self, status: str) -> None:
        state = _STATE_LABELS[status]
        if state == "armed":
            self.last_error = None
        self._set_state(state)

    def _on_link_broken(self) -> None:
        # Pushes ready=False: the GUI stops offering to arm a dead link.
        self._set_state("idle")

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self.board_state = "idle"

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """The arm slice for headless ``octacam record``, without which the board
        is never armed and the cameras wait forever."""
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
        }

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the board when the start request holds a twophoton slice (the
        tab's "Arm with recording"); a missing fps or duration takes its default."""
        if params is None:
            return
        arm = ArmParams.from_payload(params, self._default_fps, self._default_duration_ms)
        if not self._link.is_open:
            log.warning(
                "twophoton: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        if not self.firmware_ok:
            log.error(
                "twophoton: firmware on %s (%r) is incompatible; refusing to "
                "arm — reflash with `octacam flash` or the Flash firmware button",
                self.device, self.banner,
            )
            return
        log.info("twophoton: arming at %d fps for %d ms", arm.fps, arm.duration_ms)
        result = self._link.arm(arm)
        # Shown in the GUI too, since its checkbox still reads "armed"; a
        # dropped or garbled packet is otherwise silent.
        if result == "write_failed":
            self.report_error(f"arm write to {self.device} failed")
        elif result == "timeout":
            self.report_error(
                f"no arm acknowledgement from {self.device} within {ACK_TIMEOUT_S:.1f}s; "
                "the board may not have armed (cameras could wait for a trigger "
                "that never fires)"
            )

    def on_recording_stop(self, aborted: bool) -> None:
        # Cancel on every stop: a manual stop (aborted=False) can leave the board
        # running, and an idle board ignores it. The firmware goes idle silently,
        # so the state is reset here.
        self._link.send_cancel()
        self._set_state("idle")
