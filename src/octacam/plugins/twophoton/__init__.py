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
    auto_flash = false           # headless: reflash a stale board without prompting

The plugin can also be enabled at launch time with ``--plugin twophoton``.
Its serial dependency (pyserial) ships with octacam by default, so no extra
install is needed.

**Firmware provisioning.** The firmware answers an identify query with a banner
``"2PHOTON <ver> <build>"`` (``<build>`` = a hash of ``arduino/2photon_trigger``);
octacam compares it to the source and offers to compile + upload the current
sketch (arduino-cli, board ``arduino:avr:mega``) when the board is out of date,
blank, or foreign — from the GUI's *Flash firmware* button, ``octacam flash``, or
a prompt at ``octacam record`` start. See :mod:`octacam.firmware`.

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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from octacam import firmware as fw
from octacam import serial_ports
from octacam.plugins import register
from octacam.plugins._serial_link import SerialReaderLink
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

# Mega 2560 DTR-reset boot window; TwoPhotonLink.identify
# waits this long before asking once more.
_BOOT_SETTLE_TIMEOUT_S = 2.5

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# Wire-format constants. The cancel (0xCA) / identify (0x3F) magics live in
# _serial_link (shared with the triggerbox link).
_ARM_MAGIC = 0xA5
# magic (uint8) + fps (uint16 LE) + duration_ms (uint32 LE) = 7 bytes
_ARM_FORMAT = "<BHI"

_STATUS_BYTES = frozenset(b"ATD")

# Firmware identity + provisioning (see octacam.firmware). The banner is
# "2PHOTON <version> <build>"; it deliberately starts with '2' (never a status
# byte) so the reader can tell it apart from the bare 'A'/'T'/'D' status bytes.
_EXPECTED_BANNER = "2PHOTON"
_FQBN = "arduino:avr:mega"  # Arduino Mega 2560
_PROTOCOL_VERSION = 1


def _firmware_spec() -> fw.FirmwareSpec | None:
    """The 2-photon trigger firmware spec for :mod:`octacam.firmware`, or None
    when the sketch source can't be located (a wheel install without a checkout)."""
    sketch = fw.resolve_sketch_dir("2photon_trigger")
    if sketch is None:
        return None
    return fw.FirmwareSpec(
        name="twophoton",
        sketch_dir=sketch,
        fqbn=_FQBN,
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="TWOPHOTON_FW_BUILD",
    )


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


class TwoPhotonLink(SerialReaderLink):
    """Serial link to the 2-photon trigger Arduino.

    Inherits the shared open/close/write/identify lifecycle from
    :class:`~octacam.plugins._serial_link.SerialReaderLink`; only the single-byte
    status / banner reader grammar (:meth:`_read_loop`) and the arm packet
    (:meth:`send_arm`) are 2-photon-specific. The status callback runs on the
    reader thread; callers must be thread-safe.
    """

    log_prefix = "2-photon trigger"
    reader_name = "twophoton-reader"
    expected_banner = _EXPECTED_BANNER

    def send_arm(self, params: ArmParams) -> bool:
        return self._write(params.to_bytes())

    def identify(self, timeout: float = 0.5) -> str | None:
        banner = super().identify(timeout)
        if banner is not None:
            return banner
        # Don't poll here — more queries just extend the bootloader's reset wait.
        time.sleep(_BOOT_SETTLE_TIMEOUT_S)
        return super().identify(timeout)

    def _read_loop(self) -> None:
        # The firmware emits bare single-byte statuses ('A'/'T'/'D') AND, in reply
        # to an identify query, a newline-terminated banner "2PHOTON <v> <build>".
        # The banner never starts with a status byte, so a byte seen with an empty
        # buffer is a status; anything else accumulates into the banner line.
        buf = bytearray()
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
            if not b:
                continue
            byte = b[0]
            if byte == 0x0A:  # '\n' — end of a banner line
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
    auto_flash = options.get("auto_flash", False)
    if isinstance(auto_flash, str):
        auto_flash = auto_flash.strip().lower() in ("1", "true", "yes", "on")
    return TwoPhotonPlugin(
        device=str(device),
        baud=baud,
        default_fps=default_fps,
        default_duration_ms=default_duration_ms,
        auto_flash=bool(auto_flash),
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
        auto_flash: bool = False,
    ):
        # _configured_device is what the config asked for (a path, or "auto");
        # self.device is the currently-active/display device, resolved on open.
        self._configured_device = device
        self.device = device
        self.baud = baud
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._auto_flash = bool(auto_flash)
        self._firmware: str | None = None
        self._firmware_ok = True
        self._last_error: str | None = None
        self._link = TwoPhotonLink(
            self._on_arduino_status, on_broken=self._on_link_broken
        )
        # Firmware detection + flash lifecycle (shared with the other serial
        # plugins). Owns the port lock; _open/reconnect take it too. See
        # octacam.firmware.
        self._fw = fw.FirmwareProvisioner(
            _firmware_spec(),
            resolve_device=lambda: serial_ports.resolve_device(self._configured_device, self.baud),
            reopen=self._open,
            close_link=lambda: self._link.close(),
            wait_for_device=serial_ports.wait_for_device,
            is_busy=self._fw_is_busy,
        )
        self._arduino_state = "idle"
        # Set by the status reader when the firmware acknowledges an arm ('A').
        # on_recording_start waits on it so a silently-dropped arm is surfaced
        # instead of leaving the cameras waiting on a trigger that never fires.
        self._armed_event = threading.Event()
        self._ack_timeout_s = ACK_TIMEOUT_S
        # Whether the operator's "Arm with recording" checkbox was checked for
        # the take currently in progress (or the one that just finished) —
        # regardless of whether the arm write/ack actually succeeded, since the
        # operator's intent to run a synchronized 2P take is the signal the
        # transfer pipeline needs (recording_metadata), not the handshake
        # outcome. Read back by recording_metadata(), never recomputed there.
        self._armed_this_take = False
        # Injected by app.py via set_broadcast() once the web app is created.
        self._broadcast: Callable[[str, dict], None] | None = None

    def _fw_is_busy(self) -> tuple[bool, str]:
        """Refuse to flash while the trigger is armed or a capture is running."""
        if self._arduino_state in ("armed", "triggered"):
            return True, "refusing to flash while the trigger is armed/running — stop the recording first"
        return False, ""

    # -------------------------------------------------- broadcast injection

    def set_broadcast(self, callback: Callable[[str, dict], None]) -> None:
        """Inject the WebSocket broadcast hook (called by app.py at startup)."""
        self._broadcast = callback

    def _on_arduino_status(self, status: str) -> None:
        state = _STATE_LABELS.get(status, "idle")
        if state == "armed":
            self._armed_event.set()  # release a pending on_recording_start ack wait
            self._last_error = None  # a good arm clears a prior arm-failure notice
        self._set_arduino_state(state)

    def _on_link_broken(self) -> None:
        """Reader-thread hook: the serial port died mid-session. Re-broadcast the
        state so the GUI sees ``ready=False`` and disables the arm gate (and shows
        the reconnect notice) instead of carrying a stale ``ready`` that would arm
        a dead link on the next recording."""
        self._set_arduino_state("idle")

    def _set_arduino_state(self, state: str) -> None:
        self._arduino_state = state
        self._broadcast_state()

    def _broadcast_state(self) -> None:
        if self._broadcast is None:
            return
        # Carry link readiness + firmware state with every push so a client that
        # connected before the port opened (or after it died) keeps its arm gate
        # and flash prompt in sync without a separate poll.
        check = self._fw.check
        self._broadcast(
            "twophoton_state",
            {
                "state": self._arduino_state,
                "device": self.device,
                "ready": self._link.is_open,
                "firmware": self._firmware,
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
        """(Re)open the serial link; returns an error message on failure, else None.

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
        """Fallback when the sketch source is unavailable (no fingerprint):
        compatible unless the banner is a foreign name or a different version."""
        if not banner:
            return True
        name, version, _ = fw.parse_banner(banner)
        if name != _EXPECTED_BANNER.upper():
            return False
        return version is None or version == _PROTOCOL_VERSION

    def _verify_identity(self) -> None:
        """Read the firmware banner and classify it against the sketch source.

        An OUTDATED or UNIDENTIFIED board still arms fine (the arm protocol is
        unchanged); a foreign banner or wrong version disables arming and offers a
        reflash."""
        banner = self._link.identify()
        self._firmware = banner
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
            "firmware": self._firmware,
            "firmware_ok": self._firmware_ok,
            "firmware_state": check.state.value if check else None,
            "needs_flash": bool(check and check.needs_flash),
            "error": self._last_error,
        }

    # -------------------------------------------------- firmware provisioning

    def firmware_provisioning(self) -> dict:
        """Firmware picture for `octacam flash` and the GUI."""
        return self._fw.provisioning(
            plugin_name=self.name,
            device=self.device,
            firmware=self._firmware,
            firmware_ok=self._firmware_ok,
            extra={"auto_flash": self._auto_flash},
        )

    def flash_firmware(self, on_line: Callable[[str], None] | None = None) -> fw.FlashResult:
        """Compile + upload the current 2-photon trigger firmware (arduino:avr:mega).

        Delegates the close→upload→reopen→re-verify lifecycle to the shared
        FirmwareProvisioner. Never raises."""
        result = self._fw.flash(on_line=on_line)
        if result.ok:
            self._arduino_state = "idle"
        else:
            self._last_error = result.message
        self._broadcast_state()
        return result

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Headless (CLI) arm slice for ``octacam record``.

        The GUI supplies this slice from its tab; ``octacam record`` has no UI, so
        contribute it here (built from the recording's fps/duration) or the board
        is never armed and the external-triggered cameras hang forever. Only
        fps/duration_ms are consumed (ArmParams.from_payload); the plugin's
        configured defaults still apply as per-field fallbacks."""
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
        }

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the Arduino when the GUI's "Arm with recording" checkbox is checked.

        Only arms when ``params["twophoton"]`` is present — its absence means the
        operator left the checkbox unchecked.  ``fps`` and ``duration_ms`` inside
        that dict are optional; they fall back to the plugin's configured defaults.
        """
        spec = (params or {}).get("twophoton")
        self._armed_this_take = spec is not None
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
        if not self._firmware_ok:
            log.error(
                "2-photon trigger: firmware on %s (%r) is incompatible; refusing to "
                "arm — reflash with `octacam flash` or the Flash firmware button",
                self.device, self._firmware,
            )
            return
        log.info(
            "2-photon trigger: arming at %d fps for %d ms", arm.fps, arm.duration_ms
        )
        self._armed_event.clear()
        if not self._link.send_arm(arm):
            # The write never reached the OS (wedged/closed link). Surface it to
            # the GUI, not just the log, or the operator sees an "armed" checkbox
            # while the cameras wait on a trigger that never fires.
            self._last_error = f"arm write to {self.device} failed"
            log.warning("2-photon trigger: %s", self._last_error)
            self._broadcast_state()
            return
        # Wait briefly for the firmware's 'A' acknowledgement. A dropped or
        # garbled arm packet (or one whose payload arrives too late for the
        # firmware's parse window) otherwise fails silently and the cameras wait
        # on an external trigger that never fires; surface it so the operator knows.
        if self._ack_timeout_s > 0 and not self._armed_event.wait(self._ack_timeout_s):
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

    def recording_metadata(self) -> dict:
        """Whether this take was armed, for the twophoton-transfer matcher.

        ``armed`` reflects the operator's checkbox, not the handshake outcome
        (see ``_armed_this_take``) — it tells the transfer pipeline whether to
        bother looking for a paired 2P folder at all, not whether the arm
        definitely reached the board."""
        return {"armed": self._armed_this_take}

    # -------------------------------------------------- web contributions

    def web_assets(self) -> Path:
        """The plugin's co-located JS/CSS folder, served at /plugins/twophoton/."""
        return Path(__file__).parent / "web"

    def api_router(self):
        from fastapi import APIRouter, Body

        router = APIRouter()

        @router.post("/api/twophoton/reconnect")
        def reconnect(payload: dict = Body(default={})):
            """Re-attempt opening the serial port after an unplug/replug.

            An optional ``{"device": "/dev/…"}`` body switches to a different
            port before reopening; with no body it reopens the configured one."""
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self._configured_device = device.strip()
            error = self._open()
            check = self._fw.check
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "error": error,
                "arduino_state": self._arduino_state,
                "firmware": self._firmware,
                "firmware_ok": self._firmware_ok,
                "firmware_state": check.state.value if check else None,
                "needs_flash": bool(check and check.needs_flash),
            }

        @router.get("/api/twophoton/status")
        def get_status():
            """Current connection and Arduino state (for initial page load)."""
            return {"ready": self._link.is_open, **self.status()}

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
                "firmware": self._firmware,
                "firmware_ok": self._firmware_ok,
                "ready": self._link.is_open,
                "provisioning": self.firmware_provisioning(),
            }

        return router
