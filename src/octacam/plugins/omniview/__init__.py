"""omniview rig trigger + LED-strobe plugin (opt-in).

Drives the Arduino Nano ESP32 running the ``omniview`` firmware (see
``arduino/omniview_trigger/``): on recording start it arms the board over serial
with the recording's fps + duration and the configured **strobe duty cycle**; the
board then emits the shared camera trigger (D13 → all 4 Basler + 2 FLIR) and
strobes the two CCS LED ring lights (channels 1 & 2) in sync, for the duration.
On recording stop it cancels the board.

Enable it with a ``[[plugins]]`` entry in ``octacam_config.toml`` (options go
under a ``[plugins.options]`` sub-table)::

    [[plugins]]
    name = "omniview"

    [plugins.options]
    device = "/dev/ttyACM0"    # udev symlink or /dev/ttyACM0, COM3, etc.
    baud = 115200              # optional; default 115200
    default_duty_percent = 20  # manual LED strobe duty (% of the frame period)
    default_duty_auto = false  # size the strobe from the live camera exposures
    strobe_guard_us = 100      # guard band added to the longest exposure (auto)
    default_cam_pulse_us = 0   # camera trigger pulse width µs (0 = firmware default)
    default_fps = 80           # fallback when the GUI params are not sent
    default_duration_ms = 10000

When ``default_duty_auto`` (or the GUI's Auto mode) is on, the strobe on-time is
computed as ``max(TriggerDelay + ExposureTime over all cameras) + strobe_guard_us``
and converted to a duty for the recording's fps — so the LED is guaranteed to
cover the longest exposure regardless of the manual duty percent.

The plugin can also be enabled at launch with ``--plugin omniview``. Its serial
dependency (pyserial) ships with octacam, so no extra install is needed. The cameras
must be configured for external hardware trigger (``trigger_source = "external"``).

Wire protocol (host → Arduino, little-endian):
  [0xA5][fps u16][duration_ms u32][duty_permille u16][cam_pulse_us u16]  — arm (11 B)
  [0xCA]                                                                 — cancel
  [0x3F] '?'                                                             — identify

Wire protocol (Arduino → host, newline-terminated ASCII tokens):
  "R" running · "D" done · "C" cancelled/idle · "OMNIVIEW <version>" identify reply

Arduino state changes are broadcast over the GUI WebSocket so the operator sees
real-time feedback without polling.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octacam.controller import RecordingController

from octacam import serial_ports
from octacam.plugins import register
from octacam.plugins.base import Plugin

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 80
DEFAULT_DURATION_MS = 10_000
DEFAULT_DUTY_PERCENT = 20.0
DEFAULT_CAM_PULSE_US = 0  # 0 → firmware default pulse width
# Auto strobe duty: when on, the LED on-time is sized from the live camera
# exposures instead of the manual duty percent (see _with_auto_duty).
DEFAULT_DUTY_AUTO = False
# Guard band (µs) added on top of the longest (TriggerDelay + ExposureTime) when
# auto duty computes the LED on-time. It covers the camera's trigger-to-exposure
# latency and any exposure jitter so the strobe safely brackets the exposure's
# trailing edge; the leading edge is covered by each camera's TriggerDelay (the
# LED rises with the trigger, before the delayed exposure starts). The Arduino's
# own frame-clock jitter is sub-µs, so this guards the *cameras*, not the board.
DEFAULT_STROBE_GUARD_US = 100

# How long on_recording_start waits for the firmware's 'R' acknowledgement before
# warning that the arm may not have taken. The firmware acks within a few ms; the
# wait runs off the controller lock, so it only delays the start response.
ACK_TIMEOUT_S = 1.0

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# Wire-format constants (must match omniview_trigger.ino).
_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
_IDENTIFY_MAGIC = 0x3F
# magic u8 + fps u16 + duration_ms u32 + duty_permille u16 + cam_pulse_us u16
_ARM_FORMAT = "<BHIHH"
_MAX_FPS = 5000

# Status tokens the firmware emits (newline-terminated); mapped to a UI state.
_STATE_LABELS: dict[str, str] = {
    "R": "running",
    "D": "done",
    "C": "idle",
}

# Prefix of the firmware identity banner (kVersion = "OMNIVIEW <n>" in the .ino).
# A connected board whose banner doesn't start with this is likely the wrong one.
_EXPECTED_BANNER = "OMNIVIEW"


@dataclass
class ArmParams:
    fps: int
    duration_ms: int
    duty_permille: int
    cam_pulse_us: int

    def to_bytes(self) -> bytes:
        return struct.pack(
            _ARM_FORMAT,
            _ARM_MAGIC,
            self.fps,
            self.duration_ms,
            self.duty_permille,
            self.cam_pulse_us,
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict,
        default_fps: int,
        default_duration_ms: int,
        default_duty_percent: float,
        default_cam_pulse_us: int,
    ) -> ArmParams:
        """Build from a plugin_params dict; missing keys fall back to defaults."""

        def _int(key: str, default: int) -> int:
            try:
                return int(payload.get(key, default))
            except (TypeError, ValueError):
                return default

        def _float(key: str, default: float) -> float:
            try:
                return float(payload.get(key, default))
            except (TypeError, ValueError):
                return default

        fps = max(1, min(_MAX_FPS, _int("fps", default_fps)))
        # Clamp to the uint32 wire field, mirroring the fps clamp: an absurd
        # duration would otherwise make struct.pack raise inside send_arm, which
        # dispatch swallows — silently skipping the arm.
        duration_ms = max(1, min(0xFFFF_FFFF, _int("duration_ms", default_duration_ms)))
        duty_percent = max(
            0.0, min(100.0, _float("duty_percent", default_duty_percent))
        )
        duty_permille = int(round(duty_percent * 10))  # % → ‰ (0..1000)
        cam_pulse_us = max(0, min(0xFFFF, _int("cam_pulse_us", default_cam_pulse_us)))
        return cls(
            fps=fps,
            duration_ms=duration_ms,
            duty_permille=duty_permille,
            cam_pulse_us=cam_pulse_us,
        )


@dataclass
class CameraTiming:
    """One camera's exposure-timing slice, read live for the auto strobe duty.

    ``exposure_us`` is None when the camera can't report its ExposureTime (a
    backend that can't introspect, or a model without the node). ``coverage_us``
    is the instant, relative to the trigger edge, by which the exposure has
    ended — TriggerDelay pushes exposure start later, so the strobe must stay on
    at least this long to bracket it.
    """

    index: int
    name: str
    exposure_us: float | None
    trigger_delay_us: float

    @property
    def coverage_us(self) -> float | None:
        if self.exposure_us is None:
            return None
        return self.trigger_delay_us + self.exposure_us


class OmniviewLink:
    """Serial link to the omniview trigger Arduino.

    Writes arm/cancel packets and reads back newline-terminated status tokens on a
    dedicated background thread. The status callback runs on that thread; callers
    must be thread-safe. Mirrors ``TwoPhotonLink`` but line-based (the omniview
    firmware terminates every token with ``\\n``).
    """

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_broken: Callable[[], None] | None = None,
    ):
        self._serial = None
        self._write_lock = threading.Lock()
        # Serializes open/close/reconnect so two concurrent reconnects can't each
        # create a port and leak the loser's FD + reader.
        self._lifecycle_lock = threading.Lock()
        self._on_status = on_status
        # Called from the reader thread when the port dies mid-session (not on a
        # clean close), so the owner can surface the lost link to the GUI.
        self._on_broken = on_broken
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        # Firmware identity banner ("OMNIVIEW <version>"), captured by the reader
        # when it arrives in reply to an identify query. identify() waits on it.
        self._identity: str | None = None
        self._identity_event = threading.Event()

    def open(self, device: str, baud: int) -> None:
        if serial is None:
            raise RuntimeError(_NO_PYSERIAL_MSG)
        with self._lifecycle_lock:
            self._close_locked()
            s = serial.Serial(device, baud, timeout=0.2, write_timeout=1)
            self._serial = s
            self._reader_stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="omniview-reader"
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
        """Drop the handle after the port dies under the reader thread, so
        ``is_open`` stops reporting a dead link as usable and the GUI offers
        reconnect. Touches only ``_write_lock`` (never the lifecycle lock) so it
        can't deadlock a concurrent close() joining us."""
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        if self._on_broken is not None:
            try:
                self._on_broken()
            except Exception:
                log.exception("omniview trigger: on_broken callback error")

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
                log.warning("omniview trigger: serial write failed: %s", e)

    def send_arm(self, params: ArmParams) -> None:
        self._write(params.to_bytes())

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def send_identify(self) -> None:
        self._write(bytes([_IDENTIFY_MAGIC]))

    @property
    def identity(self) -> str | None:
        """The last firmware banner captured, or None if none seen yet."""
        return self._identity

    def identify(self, timeout: float = 0.5) -> str | None:
        """Query the firmware banner and wait briefly for the reply.

        Returns the banner (e.g. ``"OMNIVIEW 1"``) or None if the board did not
        answer in time (an older firmware, a wrong board, or a slow link)."""
        self._identity = None
        self._identity_event.clear()
        self.send_identify()
        self._identity_event.wait(timeout)
        return self._identity

    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._reader_stop.is_set():
            s = self._serial
            if s is None or not s.is_open:
                break
            try:
                chunk = s.read(64)
            except serial.SerialException:  # pyright: ignore[reportOptionalMemberAccess]
                # Port died under us (e.g. unplugged mid-run). Drop the handle so
                # is_open turns False and the GUI surfaces reconnect, unless we
                # are already shutting down cleanly.
                if not self._reader_stop.is_set():
                    self._mark_broken()
                break
            except Exception:
                # The port was closed under us (fd → None during shutdown),
                # surfacing as TypeError from os.read(None, ...). Any unexpected
                # exception must also not crash the daemon thread.
                if not self._reader_stop.is_set():
                    log.debug(
                        "omniview trigger: read error in reader thread", exc_info=True
                    )
                    self._mark_broken()
                break
            if not chunk:
                continue
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                del buf[:]
                buf.extend(rest)
                token = line.decode("ascii", "replace").strip()
                if token.upper().startswith("OMNIVIEW"):
                    # Identity reply to an identify query; release identify().
                    self._identity = token
                    self._identity_event.set()
                elif token in _STATE_LABELS:
                    try:
                        self._on_status(token)
                    except Exception:
                        log.exception("omniview trigger: status callback error")


@register("omniview")
def _build(options: dict) -> OmniviewPlugin:
    if serial is None:
        raise RuntimeError(_NO_PYSERIAL_MSG)

    def _opt_int(key: str, default: int) -> int:
        try:
            return int(options.get(key, default))
        except (TypeError, ValueError):
            log.warning(
                "omniview plugin: invalid %s %r; using %d",
                key,
                options.get(key),
                default,
            )
            return default

    def _opt_float(key: str, default: float) -> float:
        try:
            return float(options.get(key, default))
        except (TypeError, ValueError):
            log.warning(
                "omniview plugin: invalid %s %r; using %g",
                key,
                options.get(key),
                default,
            )
            return default

    def _opt_bool(key: str, default: bool) -> bool:
        val = options.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        try:
            return bool(int(val))
        except (TypeError, ValueError):
            log.warning(
                "omniview plugin: invalid %s %r; using %s", key, val, default
            )
            return default

    return OmniviewPlugin(
        device=str(options.get("device") or DEFAULT_DEVICE),
        baud=_opt_int("baud", DEFAULT_BAUD),
        default_fps=_opt_int("default_fps", DEFAULT_FPS),
        default_duration_ms=_opt_int("default_duration_ms", DEFAULT_DURATION_MS),
        default_duty_percent=_opt_float("default_duty_percent", DEFAULT_DUTY_PERCENT),
        default_duty_auto=_opt_bool("default_duty_auto", DEFAULT_DUTY_AUTO),
        strobe_guard_us=_opt_int("strobe_guard_us", DEFAULT_STROBE_GUARD_US),
        default_cam_pulse_us=_opt_int("default_cam_pulse_us", DEFAULT_CAM_PULSE_US),
    )


class OmniviewPlugin(Plugin):
    """omniview rig trigger + LED-strobe plugin.

    Arms the Arduino with the recording's fps + duration and the strobe duty
    cycle, then the board free-runs the camera trigger and synchronized LED strobe
    for the duration. Board state changes are broadcast over the GUI WebSocket.
    """

    name = "omniview"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        default_duty_percent: float = DEFAULT_DUTY_PERCENT,
        default_duty_auto: bool = DEFAULT_DUTY_AUTO,
        strobe_guard_us: int = DEFAULT_STROBE_GUARD_US,
        default_cam_pulse_us: int = DEFAULT_CAM_PULSE_US,
    ):
        # _configured_device is what the config asked for (a path, or "auto");
        # self.device is the currently-active/display device, resolved on open.
        self._configured_device = device
        self.device = device
        self.baud = baud
        self._firmware: str | None = None
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._default_duty_percent = default_duty_percent
        self._default_duty_auto = default_duty_auto
        self._strobe_guard_us = max(0, strobe_guard_us)
        self._default_cam_pulse_us = default_cam_pulse_us
        # Injected by app.py / the CLI record path via set_controller. Used by the
        # auto strobe duty to read each camera's live ExposureTime + TriggerDelay.
        # None in headless unit tests and if the host never wires it.
        self._controller: RecordingController | None = None
        self._link = OmniviewLink(
            self._on_arduino_status, on_broken=self._on_link_broken
        )
        self._arduino_state = "idle"
        # Set by the reader when the firmware acknowledges an arm ('R'), so
        # on_recording_start can surface a silently-dropped arm instead of leaving
        # the cameras waiting on a trigger that never fires.
        self._armed_event = threading.Event()
        self._ack_timeout_s = ACK_TIMEOUT_S
        # Injected by app.py via set_broadcast() once the web app is created.
        self._broadcast: Callable[[str, dict], None] | None = None

    # -------------------------------------------------- broadcast injection

    def set_broadcast(self, callback: Callable[[str, dict], None]) -> None:
        """Inject the WebSocket broadcast hook (called by app.py at startup)."""
        self._broadcast = callback

    def set_controller(self, controller: RecordingController) -> None:
        """Inject the recording controller (called by app.py / the CLI record path).

        Lets the auto strobe duty read each camera's live ExposureTime and
        TriggerDelay so the LED on-time can be sized to bracket the exposures.
        """
        self._controller = controller

    # -------------------------------------------------- camera exposure timings

    @staticmethod
    def _read_exposure_us(camera) -> float | None:
        """A camera's ExposureTime in µs, or None if it can't be read."""
        try:
            value = camera.read_param("exposure").get("value")
        except Exception:
            return None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _read_trigger_delay_us(camera) -> float:
        """A camera's TriggerDelay in µs; 0.0 when the node is unavailable.

        TriggerDelay isn't a curated PARAM_NODE, so it's read via the generic
        node-map path; a model/backend that doesn't expose it delays exposure by
        nothing (0), which is the correct assumption for coverage math.
        """
        try:
            value = camera.read_feature("TriggerDelay").get("value")
        except Exception:
            return 0.0
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _camera_timings(self) -> list[CameraTiming]:
        """Live (exposure, trigger-delay) for every open camera, [] if no controller."""
        controller = self._controller
        if controller is None:
            return []
        try:
            cameras = list(enumerate(controller.camera_system))
        except Exception:
            log.debug("omniview trigger: camera enumeration failed", exc_info=True)
            return []
        timings: list[CameraTiming] = []
        for index, camera in cameras:
            timings.append(
                CameraTiming(
                    index=index,
                    name=getattr(camera, "name", "") or f"cam{index}",
                    exposure_us=self._read_exposure_us(camera),
                    trigger_delay_us=self._read_trigger_delay_us(camera),
                )
            )
        return timings

    def _auto_led_on_us(self) -> float | None:
        """LED on-time (µs) that brackets the longest exposure + guard, or None
        when no camera exposure can be read (caller then keeps the manual duty)."""
        coverages = [
            c for c in (t.coverage_us for t in self._camera_timings()) if c is not None
        ]
        if not coverages:
            return None
        return max(coverages) + self._strobe_guard_us

    def _with_auto_duty(self, arm: ArmParams) -> ArmParams:
        """Return ``arm`` with duty_permille sized to cover the longest exposure.

        Converts the auto LED on-time to a permille of the frame period for the
        arm's fps, clamped to a real pulse (≥1‰) and ≤100%. If the camera timings
        can't be read, the manual/default duty already in ``arm`` is kept.
        """
        led_on_us = self._auto_led_on_us()
        if led_on_us is None:
            log.warning(
                "omniview trigger: auto strobe duty requested but no camera "
                "exposure could be read; falling back to manual %.1f%% duty",
                arm.duty_permille / 10,
            )
            return arm
        period_us = 1_000_000.0 / arm.fps
        # Round the on-time UP (ceil), not to nearest: the firmware on-time is
        # quantised to permille (a period/1000 step), and at very low fps that
        # step can exceed the guard band — round-to-nearest could then turn the
        # LED off a few µs before the exposure ends. ceil guarantees the quantised
        # on-time is always ≥ the required led_on_us, so coverage never undershoots.
        permille = max(1, min(1000, math.ceil(led_on_us / period_us * 1000)))
        return replace(arm, duty_permille=permille)

    def _wants_auto_duty(self, spec: dict) -> bool:
        """Whether this arm should use auto duty (GUI flag, else configured default)."""
        return bool(spec.get("duty_auto", self._default_duty_auto))

    def _on_arduino_status(self, token: str) -> None:
        state = _STATE_LABELS.get(token, "idle")
        if state == "running":
            self._armed_event.set()  # release a pending on_recording_start ack wait
        self._set_arduino_state(state)

    def _on_link_broken(self) -> None:
        """Reader-thread hook: the serial port died mid-session. Re-broadcast the
        state so the GUI sees ``ready=False`` and disables the arm gate (and shows
        the reconnect notice) instead of carrying a stale ``ready``."""
        self._set_arduino_state("idle")

    def _set_arduino_state(self, state: str) -> None:
        self._arduino_state = state
        if self._broadcast is not None:
            # Carry link readiness with every state push so a client that
            # connected before the port opened (or after it died) keeps its arm
            # gate in sync without a separate poll.
            self._broadcast(
                "omniview_state",
                {
                    "state": state,
                    "device": self.device,
                    "ready": self._link.is_open,
                    "firmware": self._firmware,
                },
            )

    # -------------------------------------------------- process lifecycle

    def setup(self) -> None:
        self._open()

    def _open(self) -> str | None:
        """(Re)open the serial link; returns an error message on failure, else None.

        Resolves ``device="auto"`` to a single detected board, enriches an open
        failure with the detected candidate ports, and reads back the firmware
        identity so a wrong board on the port is surfaced instead of failing
        silently at record time."""
        self._firmware = None
        device, reason = serial_ports.resolve_device(self._configured_device, self.baud)
        if device is None:
            log.warning("omniview trigger: %s", reason)
            return reason
        if device != self.device:
            log.info("omniview trigger: %s", reason)
            self.device = device
        try:
            self._link.open(device, self.baud)
        except Exception as e:
            msg = serial_ports.explain_open_failure(device, e)
            log.warning("omniview trigger: %s", msg)
            return msg
        log.info("omniview trigger: opened %s @ %d", device, self.baud)
        self._verify_identity()
        return None

    def _verify_identity(self) -> None:
        """Read the firmware banner and warn if it isn't omniview firmware."""
        banner = self._link.identify()
        self._firmware = banner
        if banner is None:
            log.info(
                "omniview trigger: no firmware identity from %s (older firmware "
                "or non-omniview board); proceeding",
                self.device,
            )
        elif not banner.upper().startswith(_EXPECTED_BANNER):
            log.warning(
                "omniview trigger: %s reports firmware %r but an %r board was "
                "expected — wrong device?",
                self.device,
                banner,
                _EXPECTED_BANNER,
            )

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self._arduino_state = "idle"

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        return {
            "device": self.device,
            "arduino_state": self._arduino_state,
            "duty_percent": self._default_duty_percent,
            "duty_auto": self._default_duty_auto,
            "guard_us": self._strobe_guard_us,
            "cam_pulse_us": self._default_cam_pulse_us,
            "firmware": self._firmware,
        }

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Headless (CLI) arm slice for ``octacam record``.

        The CLI has no "Arm with recording" checkbox, so contribute the arm
        params unconditionally — otherwise the external-trigger cameras would wait
        forever for a trigger the board is never told to send. Mirrors the GUI's
        ``getStartParams()``: the recording's fps/duration plus the configured
        strobe duty and camera-pulse width.
        """
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
            "duty_percent": self._default_duty_percent,
            "duty_auto": self._default_duty_auto,
            "cam_pulse_us": self._default_cam_pulse_us,
        }

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the Arduino when the GUI's "Arm with recording" box is checked.

        Only arms when ``params["omniview"]`` is present (its absence means the
        box was left unchecked). ``fps``/``duration_ms`` come from the recording;
        ``duty_percent``/``cam_pulse_us`` fall back to the plugin defaults.
        """
        spec = (params or {}).get("omniview")
        if spec is None:
            return
        arm = ArmParams.from_payload(
            spec,
            self._default_fps,
            self._default_duration_ms,
            self._default_duty_percent,
            self._default_cam_pulse_us,
        )
        # Auto strobe duty overrides the manual percent: size the LED on-time
        # from the live camera exposures so it brackets the longest one.
        auto = self._wants_auto_duty(spec)
        if auto:
            arm = self._with_auto_duty(arm)
        if not self._link.is_open:
            # send_arm would silently no-op on a closed link, leaving the cameras
            # waiting on an external trigger that never fires. Surface it instead.
            log.warning(
                "omniview trigger: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        log.info(
            "omniview trigger: arming at %d fps for %d ms, %.1f%% strobe duty%s",
            arm.fps,
            arm.duration_ms,
            arm.duty_permille / 10,
            " (auto: sized to longest exposure)" if auto else "",
        )
        self._armed_event.clear()
        self._link.send_arm(arm)
        # Wait briefly for the firmware's 'R' acknowledgement. A dropped or garbled
        # arm otherwise fails silently and the cameras wait on a trigger that never
        # fires; warn so the operator knows.
        if self._ack_timeout_s > 0 and not self._armed_event.wait(self._ack_timeout_s):
            log.warning(
                "omniview trigger: no run acknowledgement from %s within %.1f s; "
                "the board may not have armed (cameras could wait for a trigger "
                "that never fires)",
                self.device,
                self._ack_timeout_s,
            )

    def on_recording_stop(self, aborted: bool) -> None:
        # Stop the hardware trigger whenever a recording ends — abort, manual early
        # stop, or clean duration-elapsed finish. A manual stop arrives with
        # aborted=False while the board may still be RUNNING, so cancelling only on
        # abort would leave it pulsing for its full configured duration after the
        # cameras stopped. A cancel to an already-idle board is a harmless no-op.
        self._link.send_cancel()
        self._set_arduino_state("idle")

    # -------------------------------------------------- web contributions

    def web_assets(self) -> Path:
        """The plugin's co-located JS/CSS folder, served at /plugins/omniview/."""
        return Path(__file__).parent / "web"

    def api_router(self):
        from fastapi import APIRouter, Body

        router = APIRouter()

        @router.post("/api/omniview/reconnect")
        def reconnect(payload: dict = Body(default={})):
            """Re-attempt opening the serial port after an unplug/replug.

            An optional ``{"device": "/dev/…"}`` body switches to a different
            port (e.g. one picked from the GUI dropdown) before reopening; with
            no body it reopens the configured device."""
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self._configured_device = device.strip()
            error = self._open()
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "error": error,
                "arduino_state": self._arduino_state,
                "firmware": self._firmware,
            }

        @router.get("/api/omniview/status")
        def get_status():
            """Current connection and board state (for initial page load)."""
            return {
                "ready": self._link.is_open,
                "device": self.device,
                "arduino_state": self._arduino_state,
                "duty_percent": self._default_duty_percent,
                "duty_auto": self._default_duty_auto,
                "guard_us": self._strobe_guard_us,
                "cam_pulse_us": self._default_cam_pulse_us,
                "firmware": self._firmware,
            }

        @router.get("/api/omniview/exposures")
        def get_exposures():
            """Live per-camera exposure timings for the tab's timing visualization
            and auto-duty preview.

            ``cameras`` is empty when no controller is wired (e.g. isolated
            tests). ``guard_us`` is the guard band auto duty adds on top of the
            longest (TriggerDelay + ExposureTime).
            """
            timings = self._camera_timings()
            return {
                "guard_us": self._strobe_guard_us,
                "duty_auto_default": self._default_duty_auto,
                "cam_pulse_us": self._default_cam_pulse_us,
                "cameras": [
                    {
                        "index": t.index,
                        "name": t.name,
                        "exposure_us": t.exposure_us,
                        "trigger_delay_us": t.trigger_delay_us,
                    }
                    for t in timings
                ],
            }

        return router
