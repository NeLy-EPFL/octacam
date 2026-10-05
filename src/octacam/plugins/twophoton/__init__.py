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

import serial

from octacam import firmware as fw
from octacam import serial_ports
from octacam.plugins._serial_link import SerialReaderLink
from octacam.plugins.base import Plugin

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/arduinoCams"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 100
DEFAULT_DURATION_MS = 10_000

# How long an arm waits for the board's 'A' (sent within ms) before reporting it.
ACK_TIMEOUT_S = 1.0

# SerialReaderLink sends the cancel and identify bytes.
_ARM_MAGIC = 0xA5
_ARM_FORMAT = "<BHI"

_STATUS_BYTES = frozenset(b"ATD")

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
        # Within the wire field: a struct.error in send_arm would skip the arm.
        duration_ms = max(1, min(0xFFFF_FFFF, duration_ms))
        return cls(fps=fps, duration_ms=duration_ms)


class TwoPhotonLink(SerialReaderLink):
    """The serial link to the 2-photon trigger."""

    log_prefix = "2-photon trigger"
    reader_name = "twophoton-reader"
    expected_banner = _EXPECTED_BANNER

    def send_arm(self, params: ArmParams) -> bool:
        return self._write(params.to_bytes())

    def _read_loop(self) -> None:
        # Bare status bytes, plus the newline-terminated banner in reply to
        # identify: a status byte with an empty buffer is a status, anything
        # else builds the banner line.
        buf = bytearray()
        while not self._reader_stop.is_set():
            s = self._serial
            if s is None or not s.is_open:
                break
            try:
                b = s.read(1)
            except serial.SerialException:
                if not self._reader_stop.is_set():
                    self._mark_broken()
                break
            except Exception:  # a port closed under us: os.read(None, 1)
                if not self._reader_stop.is_set():
                    log.debug(
                        "2-photon trigger: read error in reader thread", exc_info=True
                    )
                    self._mark_broken()
                break
            if not b:
                continue
            byte = b[0]
            if byte == 0x0A:  # end of a banner line
                line = buf.decode("ascii", "replace").strip()
                buf.clear()
                if line.upper().startswith(self.expected_banner):
                    self._identity = line
                    self._identity_event.set()
                continue
            if buf:
                buf.append(byte)
                if len(buf) > 64:  # runaway/garbled line guard
                    buf.clear()
                continue
            if byte in _STATUS_BYTES:
                self._dispatch(self._on_status, chr(byte))
            else:
                buf.append(byte)  # start of a banner line


_STATE_LABELS: dict[str, str] = {
    "A": "armed",
    "T": "triggered",
    "D": "done",
}


class TwoPhotonPlugin(Plugin):
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

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        auto_flash: bool = False,
    ):
        # What the config names (a path or "auto"), and the port it resolved to.
        self.configured_device = device
        self.device = device
        self.baud = baud
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._auto_flash = bool(auto_flash)
        self._banner: str | None = None
        self._firmware_ok = True
        self._last_error: str | None = None
        self._link = TwoPhotonLink(
            self._on_arduino_status, on_broken=self._on_link_broken
        )
        assert self.firmware is not None
        self._fw = fw.FirmwareProvisioner(
            self.firmware,
            resolve_device=lambda: serial_ports.resolve_device(self.configured_device),
            reopen=self._open,
            close_link=lambda: self._link.close(),
            wait_for_device=serial_ports.wait_for_device,
            is_busy=self._fw_is_busy,
        )
        self._arduino_state = "idle"
        self._armed_event = threading.Event()
        self._ack_timeout_s = ACK_TIMEOUT_S

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

    def _fw_is_busy(self) -> tuple[bool, str]:
        """Refuse to flash while the trigger is armed or a capture is running."""
        if self._arduino_state in ("armed", "triggered"):
            return True, "refusing to flash while the trigger is armed/running — stop the recording first"
        return False, ""

    def _on_arduino_status(self, status: str) -> None:
        state = _STATE_LABELS.get(status, "idle")
        if state == "armed":
            self._armed_event.set()
            self._last_error = None
        self._set_arduino_state(state)

    def _on_link_broken(self) -> None:
        """The port died (reader thread): push ready=False, so the GUI stops
        offering to arm a dead link and offers a reconnect."""
        self._set_arduino_state("idle")

    def _set_arduino_state(self, state: str) -> None:
        self._arduino_state = state
        self._broadcast_state()

    def _broadcast_state(self) -> None:
        # Every push carries readiness and firmware state: no client polls.
        check = self._fw.check
        self.broadcast(
            "twophoton_state",
            {
                "state": self._arduino_state,
                "device": self.device,
                "ready": self._link.is_open,
                "firmware": self._banner,
                "firmware_ok": self._firmware_ok,
                "firmware_state": check.state.value if check else None,
                "needs_flash": bool(check and check.needs_flash),
                "error": self._last_error,
            },
        )

    # -------------------------------------------------- process lifecycle

    def setup(self) -> None:
        self._open()

    def _open(self) -> str | None:
        """(Re)open the link and read the banner; the error message, or None."""
        with self._fw.port_lock:
            self._banner = None
            self._firmware_ok = True
            self._last_error = None
            device, reason = serial_ports.resolve_device(self.configured_device)
            if device is None:
                log.warning("2-photon trigger: %s", reason)
                return reason
            if device != self.device:
                log.info("2-photon trigger: %s", reason)
                self.device = device
            try:
                self._link.open(device, self.baud)
            except Exception as e:
                msg = serial_ports.explain_open_failure(device, e)
                log.warning("2-photon trigger: %s", msg)
                return msg
            log.info("2-photon trigger: opened %s @ %d", device, self.baud)
            self._verify_identity()
            return None

    def _banner_arm_compatible(self, banner: str | None) -> bool:
        """Without the sketch source: compatible unless the banner names another
        firmware or protocol version."""
        if not banner:
            return True
        name, version, _ = fw.parse_banner(banner)
        if name != _EXPECTED_BANNER.upper():
            return False
        return version is None or version == _PROTOCOL_VERSION

    def _verify_identity(self) -> None:
        """Classify the board's banner: an OUTDATED or UNIDENTIFIED board still
        arms, a foreign or wrong-version one does not."""
        banner = self._link.identify()
        self._banner = banner
        check = self._fw.classify(banner)
        if check is None:
            self._firmware_ok = self._banner_arm_compatible(banner)
            return
        self._firmware_ok = fw.arm_compatible(check)
        if check.needs_flash and check.state is not fw.FirmwareState.OUTDATED:
            log.warning("2-photon trigger: %s — %s; run `octacam flash` to update.",
                        self.device, check.detail)

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self._arduino_state = "idle"

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        check = self._fw.check
        return {
            "device": self.device,
            "arduino_state": self._arduino_state,
            "firmware": self._banner,
            "firmware_ok": self._firmware_ok,
            "firmware_state": check.state.value if check else None,
            "needs_flash": bool(check and check.needs_flash),
            "error": self._last_error,
        }

    # -------------------------------------------------- firmware provisioning

    def firmware_provisioning(self) -> dict:
        """The firmware picture for ``octacam flash`` and the GUI."""
        return self._fw.provisioning(
            plugin_name=self.name,
            device=self.device,
            firmware=self._banner,
            firmware_ok=self._firmware_ok,
            extra={"auto_flash": self._auto_flash},
        )

    def flash_firmware(self, on_line: Callable[[str], None] | None = None) -> fw.FlashResult:
        """Upload the current firmware (FirmwareProvisioner.flash). Never raises."""
        result = self._fw.flash(on_line=on_line)
        if result.ok:
            self._arduino_state = "idle"
        else:
            self._last_error = result.message
        self._broadcast_state()
        return result

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
                "2-photon trigger: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        if not self._firmware_ok:
            log.error(
                "2-photon trigger: firmware on %s (%r) is incompatible; refusing to "
                "arm — reflash with `octacam flash` or the Flash firmware button",
                self.device, self._banner,
            )
            return
        log.info(
            "2-photon trigger: arming at %d fps for %d ms", arm.fps, arm.duration_ms
        )
        self._armed_event.clear()
        if not self._link.send_arm(arm):
            # Shown in the GUI too: its checkbox still reads "armed".
            self._last_error = f"arm write to {self.device} failed"
            log.warning("2-photon trigger: %s", self._last_error)
            self._broadcast_state()
            return
        # A dropped or garbled packet is otherwise silent.
        if not self._armed_event.wait(self._ack_timeout_s):
            self._last_error = (
                f"no arm ack from {self.device} within {self._ack_timeout_s:.1f}s"
            )
            log.warning(
                "2-photon trigger: no arm acknowledgement from %s within %.1f s; "
                "the board may not have armed (cameras could wait for a trigger "
                "that never fires)",
                self.device,
                self._ack_timeout_s,
            )
            self._broadcast_state()

    def on_recording_stop(self, aborted: bool) -> None:
        # Cancel on every stop: a manual stop (aborted=False) can leave the board
        # running, and an idle board ignores it. The firmware goes idle silently,
        # so the state is reset here.
        self._link.send_cancel()
        self._set_arduino_state("idle")

    # -------------------------------------------------- web contributions

    def api_router(self):
        from fastapi import APIRouter, Body

        router = APIRouter()

        @router.post("/api/twophoton/reconnect")
        def reconnect(payload: dict = Body(default={})):
            """Reopen the port, switching to ``{"device": ...}`` when given."""
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self.configured_device = device.strip()
            error = self._open()
            check = self._fw.check
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "error": error,
                "arduino_state": self._arduino_state,
                "firmware": self._banner,
                "firmware_ok": self._firmware_ok,
                "firmware_state": check.state.value if check else None,
                "needs_flash": bool(check and check.needs_flash),
            }

        @router.get("/api/twophoton/firmware")
        def get_firmware():
            """Firmware state vs. the sketch source + whether octacam can flash it."""
            return self.firmware_provisioning()

        @router.post("/api/twophoton/flash")
        def flash(payload: dict = Body(default={})):
            """Compile + upload the current firmware, then report the new state."""
            result = self.flash_firmware()
            return {
                **result.to_dict(),
                "firmware": self._banner,
                "firmware_ok": self._firmware_ok,
                "ready": self._link.is_open,
                "provisioning": self.firmware_provisioning(),
            }

        return router
