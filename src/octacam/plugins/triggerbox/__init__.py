"""triggerbox rig trigger + light-controller plugin (opt-in).

Drives the Arduino Nano ESP32 running the ``triggerbox`` firmware (see
``arduino/triggerbox/``) — the generalized controller for the EPFL
``common-trigger-circuit`` board. On recording start it arms the board over
serial with the recording's fps + duration and a full description of every
output: one or more **camera-trigger lines** and up to **3 CCS light channels**,
each independently *off / strobe / continuous / pulse-train*. On recording stop
it cancels the board. All output pins are chosen at run time, so the rig can be
rewired by editing the config — no reflashing.

Enable it with a ``[[plugins]]`` entry in ``octacam_config.toml`` (options go
under a ``[plugins.options]`` sub-table). Camera lines and light channels are
inline-table arrays::

    [[plugins]]
    name = "triggerbox"

    [plugins.options]
    device = "auto"            # udev symlink, /dev/ttyACM0, COM3, or "auto"
    baud = 115200
    auto_flash = false         # headless: reflash a stale board without prompting
    strobe_guard_us = 100      # guard band added to the longest exposure (auto duty)
    cameras = [ { pin = "D13", pulse_us = 500 } ]
    lights = [
      { channel = 1, mode = "strobe", duty_mode = "auto" },
      { channel = 2, mode = "strobe", duty_mode = "manual", duty_percent = 20 },
      { channel = 3, mode = "off" },
    ]

If ``cameras`` / ``lights`` are omitted the plugin defaults to the classic rig:
one D13 camera line + channels 1 and 2 strobing at ``default_duty_percent``.

The three CCS channels are electrically identical, so any channel can be
illumination, optogenetic stimulation, or anything else — there is no fixed
role. A ``strobe`` channel with ``duty_mode = "auto"`` sizes its on-time to
``max(TriggerDelay + ExposureTime over all cameras) + strobe_guard_us`` from the
live camera exposures, so the LED brackets the longest exposure regardless of
fps. A ``pulse_train`` channel runs an independent clock (frequency, pulse width,
start delay, train duration) decoupled from the frame rate.

Enable at launch with ``--plugin triggerbox``. pyserial ships with octacam. The
cameras must be in external hardware trigger (``trigger_source = "external"``).

**Firmware provisioning.** The board's identify banner carries a short hash of the
sketch source (``TRIGGERBOX 2 <build>``); octacam recomputes it from
``arduino/triggerbox`` and, when the board is out of date (or blank, or running a
predecessor), offers to compile + upload the current firmware with ``arduino-cli``
— from the GUI's *Flash firmware* button, the ``octacam flash`` command, or a
prompt at ``octacam record`` start. Headless runs only warn unless ``--yes`` /
``auto_flash = true``. See :mod:`octacam.firmware`.

Wire protocol v2 (host → Arduino, little-endian):
  [0xA5][version u8=2][payload_len u16][payload][checksum u8]  — arm
  [0xCA]                                                       — cancel
  [0x3F] '?'                                                   — identify
  payload = fps u16 | duration_ms u32 | n_cam u8 | n_light u8
            | n_cam×(pin_id u8, pulse_us u16, delay_us u16)
            | n_light×(pin_id u8, mode u8, p0 u32, p1 u32, p2 u32, p3 u32)
Wire protocol (Arduino → host, newline-terminated ASCII tokens):
  "R" running · "D" done · "C" cancelled/idle · "E<c>" rejected
  · "TRIGGERBOX <version>" identify reply

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

from octacam import firmware as fw
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
DEFAULT_DUTY_AUTO = False
# Guard band (µs) added on top of the longest (TriggerDelay + ExposureTime) when
# a strobe channel is in auto-duty mode. It covers the camera's trigger-to-
# exposure latency and exposure jitter so the strobe brackets the exposure's
# trailing edge; the leading edge is covered by each camera's TriggerDelay.
DEFAULT_STROBE_GUARD_US = 100

# How long on_recording_start waits for the firmware's 'R' (or 'E') response.
ACK_TIMEOUT_S = 1.0

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# ---- Wire protocol v2 (must match triggerbox.ino) --------------------------
_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
_IDENTIFY_MAGIC = 0x3F
_PROTOCOL_VERSION = 2
_MAX_FPS = 5000
_MAX_CAM = 12
_MAX_LIGHT = 3

_HDR = struct.Struct("<BBH")      # magic u8, version u8, payload_len u16
_FIXED = struct.Struct("<HIBB")   # fps u16, duration_ms u32, n_cam u8, n_light u8
_CAM = struct.Struct("<BHH")      # pin_id u8, pulse_us u16, delay_us u16
_LIGHT = struct.Struct("<BBIIII")  # pin_id u8, mode u8, p0 u32, p1 u32, p2 u32, p3 u32

# Canonical pin-label table — the SINGLE SOURCE OF TRUTH on the Python side,
# mirrored byte-for-byte by kPinTable in triggerbox.ino (a unit test asserts the
# two stay in sync). Index on the wire. D2/D3/D4 drive the status LED and are
# rejected by the firmware as trigger/light outputs.
PIN_LABELS = (
    "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11",
    "D12", "D13", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
)
_RESERVED_PINS = frozenset({"D2", "D3", "D4"})
# CCS light channel (1/2/3) → default board pin.
LIGHT_PIN_BY_CHANNEL = {1: "D5", 2: "D6", 3: "D7"}

# Light modes (name ↔ wire value). "pulse" is accepted as an alias of pulse_train.
_LIGHT_MODE_IDS = {"off": 0, "strobe": 1, "continuous": 2, "pulse_train": 3, "pulse": 3}
_LIGHT_MODE_NAMES = {0: "off", 1: "strobe", 2: "continuous", 3: "pulse_train"}

# Status tokens the firmware emits (newline-terminated); mapped to a UI state.
_STATE_LABELS: dict[str, str] = {"R": "running", "D": "done", "C": "idle"}

# Firmware 'E<c>' reject reason codes → human text (see triggerbox.ino).
_REJECT_REASONS = {
    "v": "protocol version mismatch",
    "c": "checksum",
    "o": "too many cameras/lights (oversize)",
    "l": "payload length",
    "f": "fps out of range",
    "p": "unknown pin id",
    "r": "reserved pin (D2/D3/D4)",
    "d": "duplicate pin",
    "m": "unknown light mode",
}

# Firmware identity banner prefix (kVersion = "TRIGGERBOX <n>" in the .ino).
# The full banner is "TRIGGERBOX <version> <build>" where <build> is a short hash
# of the sketch source — see octacam.firmware and arduino/triggerbox/fw_build_info.h.
_EXPECTED_BANNER = "TRIGGERBOX"
# arduino-cli fully-qualified board name for the Nano ESP32 (used to (re)flash).
_FQBN = "arduino:esp32:nano_nora"
# Known predecessor firmware names that are safe to auto-upgrade to triggerbox.
_LEGACY_BANNERS = ("OMNIVIEW",)


def _firmware_spec() -> fw.FirmwareSpec | None:
    """Describe the triggerbox firmware for :mod:`octacam.firmware`, or None when
    the sketch source can't be located (a wheel install without a checkout) — in
    which case firmware detection still works from the banner, but auto-flash is
    unavailable."""
    sketch = fw.resolve_sketch_dir("triggerbox")
    if sketch is None:
        return None
    return fw.FirmwareSpec(
        name="triggerbox",
        sketch_dir=sketch,
        fqbn=_FQBN,
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="TRIGGERBOX_FW_BUILD",
        legacy_prefixes=_LEGACY_BANNERS,
    )


def _u16(v) -> int:
    return max(0, min(0xFFFF, int(v)))


def _u32(v) -> int:
    return max(0, min(0xFFFF_FFFF, int(v)))


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pin_id(label: str) -> int:
    """Index of a pin label in PIN_LABELS. Raises ValueError if unknown."""
    return PIN_LABELS.index(str(label).upper())


# ============================================================================
#  Output specifications (config templates + wire serialization)
# ============================================================================


@dataclass
class CameraLine:
    """One camera-trigger output line."""

    pin: str = "D13"
    pulse_us: int = 0  # 0 → firmware default (500 µs)
    delay_us: int = 0

    def record(self) -> tuple[int, int, int]:
        return (pin_id(self.pin), _u16(self.pulse_us), _u16(self.delay_us))

    def to_dict(self) -> dict:
        return {"pin": self.pin, "pulse_us": self.pulse_us, "delay_us": self.delay_us}


@dataclass
class LightChannel:
    """One CCS light channel and its mode-specific timing.

    All three channels are interchangeable; ``channel`` only selects the default
    pin (1→D5, 2→D6, 3→D7). ``mode`` is off/strobe/continuous/pulse_train.
    """

    channel: int = 1
    pin: str = "D5"
    mode: str = "off"
    # strobe
    duty_mode: str = "manual"  # "auto" | "manual"
    duty_percent: float = DEFAULT_DUTY_PERCENT
    delay_us: int = 0
    # pulse_train
    freq_hz: float = 10.0
    pulse_us: int = 1000
    start_delay_ms: float = 0.0
    train_ms: float = 0.0

    def wants_auto(self) -> bool:
        return self.mode == "strobe" and self.duty_mode == "auto"

    def resolve(self, period_us: float, auto_led_on_us: float | None) -> tuple:
        """(pin_id, mode, p0, p1, p2, p3) for the wire, given the frame period.

        ``auto_led_on_us`` is the exposure-derived on-time (None if unreadable);
        an auto strobe falls back to its manual duty when it is None.
        """
        pid = pin_id(self.pin)
        mode = _LIGHT_MODE_IDS.get(self.mode, 0)
        if mode == 1:  # strobe
            if self.duty_mode == "auto" and auto_led_on_us is not None:
                on_us = int(math.ceil(auto_led_on_us))
            else:
                duty = max(0.0, min(100.0, self.duty_percent))
                on_us = int(round(duty / 100.0 * period_us))
            return (pid, 1, _u32(self.delay_us), _u32(on_us), 0, 0)
        if mode == 2:  # continuous
            return (pid, 2, 0, 0, 0, 0)
        if mode == 3:  # pulse_train
            interval = int(round(1_000_000.0 / self.freq_hz)) if self.freq_hz > 0 else 0
            return (
                pid,
                3,
                _u32(self.pulse_us),
                _u32(interval),
                _u32(int(round(self.start_delay_ms * 1000))),
                _u32(int(round(self.train_ms * 1000))),
            )
        return (pid, 0, 0, 0, 0, 0)  # off

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "pin": self.pin,
            "mode": self.mode,
            "duty_mode": self.duty_mode,
            "duty_percent": self.duty_percent,
            "delay_us": self.delay_us,
            "freq_hz": self.freq_hz,
            "pulse_us": self.pulse_us,
            "start_delay_ms": self.start_delay_ms,
            "train_ms": self.train_ms,
        }


@dataclass
class ArmSpec:
    """A fully-resolved arm packet: fps + duration + wire-ready output records."""

    fps: int
    duration_ms: int
    cameras: list[tuple]  # (pin_id, pulse_us, delay_us)
    lights: list[tuple]   # (pin_id, mode, p0, p1, p2, p3)

    def to_bytes(self) -> bytes:
        payload = _FIXED.pack(self.fps, self.duration_ms, len(self.cameras), len(self.lights))
        for rec in self.cameras:
            payload += _CAM.pack(*rec)
        for rec in self.lights:
            payload += _LIGHT.pack(*rec)
        # Checksum spans version + payload_len + payload (framing/desync guard).
        body = bytes([_PROTOCOL_VERSION]) + struct.pack("<H", len(payload)) + payload
        checksum = 0
        for byte in body:
            checksum ^= byte
        return _HDR.pack(_ARM_MAGIC, _PROTOCOL_VERSION, len(payload)) + payload + bytes([checksum])


def _camera_from_dict(d, default_pulse: int) -> CameraLine | None:
    if not isinstance(d, dict):
        log.warning("triggerbox: ignoring non-table camera entry %r", d)
        return None
    pin = str(d.get("pin", "D13")).upper()
    if pin not in PIN_LABELS:
        log.warning("triggerbox: unknown camera pin %r; using D13", pin)
        pin = "D13"
    elif pin in _RESERVED_PINS:
        log.warning("triggerbox: camera pin %s is reserved for the status LED; "
                    "the board will reject it", pin)
    return CameraLine(
        pin=pin,
        pulse_us=_coerce_int(d.get("pulse_us"), default_pulse),
        delay_us=_coerce_int(d.get("delay_us"), 0),
    )


def _light_from_dict(d, default_duty_percent: float, default_duty_auto: bool) -> LightChannel | None:
    if not isinstance(d, dict):
        log.warning("triggerbox: ignoring non-table light entry %r", d)
        return None
    channel = _coerce_int(d.get("channel"), 1)
    if channel not in LIGHT_PIN_BY_CHANNEL:
        log.warning("triggerbox: light channel must be 1/2/3, got %r; using 1", channel)
        channel = 1
    pin = str(d.get("pin", LIGHT_PIN_BY_CHANNEL[channel])).upper()
    if pin not in PIN_LABELS:
        log.warning("triggerbox: unknown light pin %r; using %s", pin, LIGHT_PIN_BY_CHANNEL[channel])
        pin = LIGHT_PIN_BY_CHANNEL[channel]
    mode = str(d.get("mode", "off")).lower()
    if mode not in _LIGHT_MODE_IDS:
        log.warning("triggerbox: unknown light mode %r; using off", mode)
        mode = "off"
    mode = _LIGHT_MODE_NAMES[_LIGHT_MODE_IDS[mode]]  # canonicalize (pulse → pulse_train)
    duty_mode = str(d.get("duty_mode", "auto" if default_duty_auto else "manual")).lower()
    if duty_mode not in ("auto", "manual"):
        duty_mode = "manual"
    return LightChannel(
        channel=channel,
        pin=pin,
        mode=mode,
        duty_mode=duty_mode,
        duty_percent=_coerce_float(d.get("duty_percent"), default_duty_percent),
        delay_us=_coerce_int(d.get("delay_us"), 0),
        freq_hz=_coerce_float(d.get("freq_hz"), 10.0),
        pulse_us=_coerce_int(d.get("pulse_us"), 1000),
        start_delay_ms=_coerce_float(d.get("start_delay_ms"), 0.0),
        train_ms=_coerce_float(d.get("train_ms"), 0.0),
    )


@dataclass
class CameraTiming:
    """One camera's exposure-timing slice, read live for a strobe's auto duty."""

    index: int
    name: str
    exposure_us: float | None
    trigger_delay_us: float

    @property
    def coverage_us(self) -> float | None:
        if self.exposure_us is None:
            return None
        return self.trigger_delay_us + self.exposure_us


# ============================================================================
#  Serial link
# ============================================================================


class TriggerboxLink:
    """Serial link to the triggerbox Arduino.

    Writes arm/cancel packets and reads back newline-terminated status tokens on
    a dedicated background thread. The status/reject callbacks run on that thread;
    callers must be thread-safe.
    """

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_broken: Callable[[], None] | None = None,
        on_reject: Callable[[str], None] | None = None,
    ):
        self._serial = None
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._on_status = on_status
        self._on_broken = on_broken
        self._on_reject = on_reject
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
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
                target=self._read_loop, daemon=True, name="triggerbox-reader"
            )
            self._reader.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._reader_stop.set()
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                s.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    def _mark_broken(self) -> None:
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
                log.exception("triggerbox: on_broken callback error")

    @property
    def is_open(self) -> bool:
        s = self._serial
        return s is not None and s.is_open

    def _write(self, data: bytes) -> bool:
        """Write bytes; return whether they were handed to the OS successfully.

        A wedged USB-CDC board fails here with EPIPE (a plain OSError, which
        pyserial does not always wrap in SerialException), so both are caught and
        reported as a failed write rather than raised."""
        with self._write_lock:
            s = self._serial
            if s is None or not s.is_open:
                return False
            try:
                s.write(data)
                return True
            except (OSError, serial.SerialException) as e:  # pyright: ignore[reportOptionalMemberAccess]
                log.warning("triggerbox: serial write failed: %s", e)
                return False

    def send_arm(self, spec: ArmSpec) -> bool:
        return self._write(spec.to_bytes())

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def send_identify(self) -> None:
        self._write(bytes([_IDENTIFY_MAGIC]))

    @property
    def identity(self) -> str | None:
        return self._identity

    def identify(self, timeout: float = 0.5) -> str | None:
        """Query the firmware banner and wait briefly for the reply."""
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
                if not self._reader_stop.is_set():
                    self._mark_broken()
                break
            except Exception:
                if not self._reader_stop.is_set():
                    log.debug("triggerbox: read error in reader thread", exc_info=True)
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
                if not token:
                    continue
                if token.upper().startswith(_EXPECTED_BANNER):
                    self._identity = token
                    self._identity_event.set()
                elif token in _STATE_LABELS:
                    self._dispatch(self._on_status, token)
                elif token[0] == "E":
                    self._dispatch(self._on_reject, token[1:])

    def _dispatch(self, cb, arg) -> None:
        if cb is None:
            return
        try:
            cb(arg)
        except Exception:
            log.exception("triggerbox: status/reject callback error")


# ============================================================================
#  Plugin factory + class
# ============================================================================


@register("triggerbox")
def _build(options: dict) -> TriggerboxPlugin:
    if serial is None:
        raise RuntimeError(_NO_PYSERIAL_MSG)

    def _opt_int(key: str, default: int) -> int:
        try:
            return int(options.get(key, default))
        except (TypeError, ValueError):
            log.warning("triggerbox plugin: invalid %s %r; using %d", key, options.get(key), default)
            return default

    def _opt_float(*keys: str, default: float) -> float:
        # Accept the first present of several key spellings (fixes the historical
        # duty_percent vs default_duty_percent mismatch).
        for key in keys:
            if key in options:
                try:
                    return float(options[key])
                except (TypeError, ValueError):
                    log.warning("triggerbox plugin: invalid %s %r; using %g", key, options[key], default)
                    return default
        return default

    def _opt_bool(*keys: str, default: bool) -> bool:
        for key in keys:
            if key not in options:
                continue
            val = options[key]
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
            try:
                return bool(int(val))
            except (TypeError, ValueError):
                log.warning("triggerbox plugin: invalid %s %r; using %s", key, val, default)
                return default
        return default

    default_duty_percent = _opt_float("duty_percent", "default_duty_percent", default=DEFAULT_DUTY_PERCENT)
    default_duty_auto = _opt_bool("duty_auto", "default_duty_auto", default=DEFAULT_DUTY_AUTO)
    default_cam_pulse_us = _opt_int("cam_pulse_us", DEFAULT_CAM_PULSE_US) \
        if "cam_pulse_us" in options else _opt_int("default_cam_pulse_us", DEFAULT_CAM_PULSE_US)

    # Camera lines: explicit array, else the classic single D13 line.
    cameras: list[CameraLine] = []
    raw_cams = options.get("cameras")
    if isinstance(raw_cams, list):
        for entry in raw_cams[:_MAX_CAM]:
            cl = _camera_from_dict(entry, default_cam_pulse_us)
            if cl is not None:
                cameras.append(cl)
    elif raw_cams is not None:
        log.warning("triggerbox plugin: 'cameras' must be an array of tables; ignoring %r", raw_cams)
    if not cameras:
        cameras = [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]

    # Light channels: explicit array, else the classic ch1+ch2 strobe.
    lights: list[LightChannel] = []
    raw_lights = options.get("lights")
    if isinstance(raw_lights, list):
        for entry in raw_lights[:_MAX_LIGHT]:
            lc = _light_from_dict(entry, default_duty_percent, default_duty_auto)
            if lc is not None:
                lights.append(lc)
    elif raw_lights is None:
        dm = "auto" if default_duty_auto else "manual"
        lights = [
            LightChannel(channel=1, pin="D5", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
            LightChannel(channel=2, pin="D6", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
        ]
    else:
        log.warning("triggerbox plugin: 'lights' must be an array of tables; ignoring %r", raw_lights)

    return TriggerboxPlugin(
        device=str(options.get("device") or DEFAULT_DEVICE),
        baud=_opt_int("baud", DEFAULT_BAUD),
        auto_flash=_opt_bool("auto_flash", default=False),
        default_fps=_opt_int("default_fps", DEFAULT_FPS),
        default_duration_ms=_opt_int("default_duration_ms", DEFAULT_DURATION_MS),
        default_duty_percent=default_duty_percent,
        default_duty_auto=default_duty_auto,
        strobe_guard_us=_opt_int("strobe_guard_us", DEFAULT_STROBE_GUARD_US),
        default_cam_pulse_us=default_cam_pulse_us,
        cameras=cameras,
        lights=lights,
    )


class TriggerboxPlugin(Plugin):
    """triggerbox rig trigger + light-controller plugin.

    Arms the Arduino with the recording's fps + duration and a full description
    of every camera line and light channel, then the board free-runs them for the
    duration. Board state changes are broadcast over the GUI WebSocket.
    """

    name = "triggerbox"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        auto_flash: bool = False,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        default_duty_percent: float = DEFAULT_DUTY_PERCENT,
        default_duty_auto: bool = DEFAULT_DUTY_AUTO,
        strobe_guard_us: int = DEFAULT_STROBE_GUARD_US,
        default_cam_pulse_us: int = DEFAULT_CAM_PULSE_US,
        cameras: list[CameraLine] | None = None,
        lights: list[LightChannel] | None = None,
    ):
        self._configured_device = device
        self.device = device
        self.baud = baud
        self._firmware: str | None = None
        self._firmware_ok = True
        # Opt-in: let a headless `octacam record` auto-flash a stale board.
        self._auto_flash = bool(auto_flash)
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._default_duty_percent = default_duty_percent
        self._default_duty_auto = default_duty_auto
        self._strobe_guard_us = max(0, strobe_guard_us)
        self._default_cam_pulse_us = default_cam_pulse_us
        self._cameras: list[CameraLine] = cameras or [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]
        self._lights: list[LightChannel] = lights or []
        self._controller: RecordingController | None = None
        self._link = TriggerboxLink(
            self._on_arduino_status,
            on_broken=self._on_link_broken,
            on_reject=self._on_arduino_reject,
        )
        # Firmware detection + flash lifecycle (shared with the other serial
        # plugins). Owns the port lock; _open/reconnect take it too so a flash and
        # a (re)open can never fight over the port. See octacam.firmware.
        self._fw = fw.FirmwareProvisioner(
            _firmware_spec(),
            resolve_device=lambda: serial_ports.resolve_device(self._configured_device, self.baud),
            reopen=lambda: self._open(allow_recovery=False),
            close_link=lambda: self._link.close(),  # late-bound: link may be replaced
            wait_for_device=serial_ports.wait_for_device,
            is_busy=self._fw_is_busy,
        )
        self._arduino_state = "idle"
        self._armed_event = threading.Event()
        self._last_reject: str | None = None
        self._last_error: str | None = None
        self._ack_timeout_s = ACK_TIMEOUT_S
        self._broadcast: Callable[[str, dict], None] | None = None

    # -------------------------------------------------- injection

    def set_broadcast(self, callback: Callable[[str, dict], None]) -> None:
        self._broadcast = callback

    def set_controller(self, controller: RecordingController) -> None:
        """Inject the recording controller so auto-duty can read live exposures."""
        self._controller = controller

    # -------------------------------------------------- firmware provisioning glue

    # Thin forwarders so callers/tests can read the provisioner's state on the
    # plugin (the provisioner is the single source of truth).
    @property
    def _fw_spec(self):
        return self._fw.spec

    @_fw_spec.setter
    def _fw_spec(self, value):
        self._fw.spec = value

    @property
    def _fw_needed_build(self):
        return self._fw.needed_build

    @_fw_needed_build.setter
    def _fw_needed_build(self, value):
        self._fw.needed_build = value

    @property
    def _fw_check(self):
        return self._fw.check

    @_fw_check.setter
    def _fw_check(self, value):
        self._fw.check = value

    def _fw_is_busy(self) -> tuple[bool, str]:
        """Refuse to flash while a recording is live or the board is armed.

        Prefers the controller's authoritative recording state (which flips before
        the board's 'R' ack, closing the arm/flash race); falls back to the last-
        seen board state."""
        controller = self._controller
        if controller is not None and getattr(controller, "recording_active", False):
            return True, "refusing to flash while a recording is active — stop it first"
        if self._arduino_state == "running":
            return True, "refusing to flash while the board is armed/running — stop the recording first"
        return False, ""

    # -------------------------------------------------- camera exposure timings

    @staticmethod
    def _read_exposure_us(camera) -> float | None:
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
        try:
            value = camera.read_feature("TriggerDelay").get("value")
        except Exception:
            return 0.0
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _camera_timings(self) -> list[CameraTiming]:
        controller = self._controller
        if controller is None:
            return []
        try:
            cameras = list(enumerate(controller.camera_system))
        except Exception:
            log.debug("triggerbox: camera enumeration failed", exc_info=True)
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
        """On-time (µs) bracketing the longest exposure + guard, or None if no
        camera exposure can be read."""
        coverages = [
            c for c in (t.coverage_us for t in self._camera_timings()) if c is not None
        ]
        if not coverages:
            return None
        return max(coverages) + self._strobe_guard_us

    # -------------------------------------------------- arm-spec assembly

    def _build_arm_spec(
        self, fps: int, duration_ms: int, cams: list[CameraLine], lights: list[LightChannel]
    ) -> ArmSpec:
        period_us = 1_000_000.0 / max(1, fps)
        auto_led_on_us: float | None = None
        if any(lt.wants_auto() for lt in lights):
            auto_led_on_us = self._auto_led_on_us()
            if auto_led_on_us is None:
                log.warning(
                    "triggerbox: auto strobe duty requested but no camera exposure "
                    "could be read; falling back to the manual duty percent"
                )
        cam_recs = [c.record() for c in cams[:_MAX_CAM]]
        light_recs = [lt.resolve(period_us, auto_led_on_us) for lt in lights[:_MAX_LIGHT]]
        return ArmSpec(fps=fps, duration_ms=duration_ms, cameras=cam_recs, lights=light_recs)

    def _cameras_from_spec(self, spec: dict) -> list[CameraLine]:
        raw = spec.get("cameras")
        if isinstance(raw, list):
            out: list[CameraLine] = []
            for entry in raw[:_MAX_CAM]:
                cl = _camera_from_dict(entry, self._default_cam_pulse_us)
                if cl is not None:
                    out.append(cl)
            return out
        return [replace(c) for c in self._cameras]

    def _lights_from_spec(self, spec: dict) -> list[LightChannel]:
        raw = spec.get("lights")
        if isinstance(raw, list):
            out: list[LightChannel] = []
            for entry in raw[:_MAX_LIGHT]:
                lc = _light_from_dict(entry, self._default_duty_percent, self._default_duty_auto)
                if lc is not None:
                    out.append(lc)
            return out
        # Legacy v1 GUI/config shape (duty_percent/duty_auto, no lights[]): keep the
        # configured channels but apply the legacy duty override to strobe ones.
        lights = [replace(lt) for lt in self._lights]
        if "duty_percent" in spec or "duty_auto" in spec:
            auto = bool(spec.get("duty_auto", self._default_duty_auto))
            duty = _coerce_float(spec.get("duty_percent"), self._default_duty_percent)
            for lt in lights:
                if lt.mode == "strobe":
                    lt.duty_mode = "auto" if auto else "manual"
                    lt.duty_percent = duty
        return lights

    # -------------------------------------------------- state / broadcast

    def _on_arduino_status(self, token: str) -> None:
        state = _STATE_LABELS.get(token, "idle")
        if state == "running":
            self._armed_event.set()
        self._set_arduino_state(state)

    def _on_arduino_reject(self, code: str) -> None:
        self._last_reject = code
        self._armed_event.set()  # unblock the ack wait; on_recording_start reports it

    def _on_link_broken(self) -> None:
        self._set_arduino_state("idle")

    def _set_arduino_state(self, state: str) -> None:
        self._arduino_state = state
        if state == "running":
            self._last_error = None  # a successful arm clears any prior failure
        self._broadcast_state()

    def _report_error(self, msg: str) -> None:
        """Log an arm/link failure loudly and push it to the GUI so an operator
        sees *why* a recording will get no triggers instead of a silent hang."""
        log.error("triggerbox: %s", msg)
        self._last_error = msg
        self._broadcast_state()

    def _broadcast_state(self) -> None:
        if self._broadcast is not None:
            check = self._fw_check
            self._broadcast(
                "triggerbox_state",
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

    def _open(self, *, allow_recovery: bool = True) -> str | None:
        # Held across the whole open+verify so a concurrent flash (which also holds
        # this lock across close→upload→reopen) can't fight over the port. Re-
        # entrant: flash's own reopen() calls this on the same thread.
        with self._fw.port_lock:
            self._firmware = None
            self._firmware_ok = True
            self._last_error = None  # a fresh connection attempt clears stale failures
            device, reason = serial_ports.resolve_device(self._configured_device, self.baud)
            if device is None:
                log.warning("triggerbox: %s", reason)
                return reason
            if device != self.device:
                log.info("triggerbox: %s", reason)
                self.device = device
            try:
                self._link.open(device, self.baud)
            except Exception as e:
                msg = serial_ports.explain_open_failure(device, e)
                log.warning("triggerbox: %s", msg)
                return msg
            log.info("triggerbox: opened %s @ %d", device, self.baud)
            self._verify_identity()
            # A board that opens but never answers identify may have a wedged USB-CDC
            # link (every transfer stalls with EPIPE while it stays enumerated); a
            # healthy triggerbox always replies fast. Try one host bus reset.
            if self._firmware is None and allow_recovery:
                if self._recover_usb("the board did not answer an identity query"):
                    self._verify_identity()
            return None

    def _recover_usb(self, why: str) -> bool:
        """Best-effort clear of a wedged USB link: close, host bus-reset, reopen.

        Returns whether the link is open again afterwards. The ESP32-S3 USB-CDC
        occasionally wedges (writes/control transfers stall with EPIPE while the
        port stays enumerated), which strands external-triggered recordings; a
        ``USBDEVFS_RESET`` re-initialises the link without a physical unplug."""
        device = self.device
        log.warning(
            "triggerbox: %s; attempting a USB bus reset on %s to recover", why, device
        )
        self._link.close()
        ok, msg = serial_ports.reset_usb_device(device)
        log.warning("triggerbox: %s", msg)
        if ok:
            serial_ports.wait_for_device(device, timeout=3.0)
        try:
            self._link.open(device, self.baud)
        except Exception as e:
            log.warning(
                "triggerbox: reopen after USB reset failed: %s",
                serial_ports.explain_open_failure(device, e),
            )
            return False
        if self._link.is_open:
            log.info("triggerbox: reopened %s after USB reset", device)
        return self._link.is_open

    def _banner_arm_compatible(self, banner: str | None) -> bool:
        """Fallback arm-compatibility check when the sketch source is unavailable
        (no fingerprint to classify against): compatible unless the banner is a
        foreign name or a different protocol version."""
        if not banner:
            return True
        name, version, _ = fw.parse_banner(banner)
        if name != _EXPECTED_BANNER.upper():
            return False
        return version is None or version == _PROTOCOL_VERSION

    def _verify_identity(self) -> None:
        """Read the firmware banner and classify it against the sketch source.

        Sets ``firmware_ok`` (whether the board understands our v2 arm packet) and
        ``_fw_check`` (the flash verdict, when the source is available). An
        OUTDATED board — same protocol, drifted source — stays arm-compatible; we
        only *offer* a reflash. A wrong protocol version or a foreign banner
        disables arming."""
        banner = self._link.identify()
        self._firmware = banner
        check = self._fw.classify(banner)
        if check is None:
            # No source checkout: can't fingerprint, so fall back to a plain
            # name/version compatibility check (no flash offer).
            self._firmware_ok = self._banner_arm_compatible(banner)
            if banner is None:
                log.info("triggerbox: no firmware identity from %s; proceeding", self.device)
            elif not self._firmware_ok:
                log.warning(
                    "triggerbox: %s reports firmware %r incompatible with protocol "
                    "v%d; arming is disabled", self.device, banner, _PROTOCOL_VERSION,
                )
            return
        self._firmware_ok = fw.arm_compatible(check)
        S = fw.FirmwareState
        if check.state is S.CURRENT:
            log.info("triggerbox: %s firmware %s", self.device, check.detail)
        elif check.state is S.OUTDATED:
            log.warning(
                "triggerbox: %s is out of date — %s; run `octacam flash` (or the "
                "Flash firmware button) to upload the current build. Arming still works.",
                self.device, check.detail,
            )
        elif check.state is S.UNIDENTIFIED:
            log.info(
                "triggerbox: %s sent no firmware identity (%s); proceeding",
                self.device, check.detail,
            )
        else:  # WRONG_VERSION / WRONG_BOARD — not armable
            log.warning(
                "triggerbox: %s — %s; reflash to TRIGGERBOX %d (arming is disabled). "
                "Run `octacam flash` or use the Flash firmware button.",
                self.device, check.detail, _PROTOCOL_VERSION,
            )

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self._arduino_state = "idle"

    def is_ready(self) -> bool:
        return self._link.is_open

    def status(self) -> dict:
        check = self._fw_check
        return {
            "device": self.device,
            "arduino_state": self._arduino_state,
            "firmware": self._firmware,
            "firmware_ok": self._firmware_ok,
            "firmware_state": check.state.value if check else None,
            "needs_flash": bool(check and check.needs_flash),
            "error": self._last_error,
            "guard_us": self._strobe_guard_us,
            "cameras": [c.to_dict() for c in self._cameras],
            "lights": [lt.to_dict() for lt in self._lights],
        }

    # -------------------------------------------------- firmware provisioning

    def firmware_provisioning(self) -> dict:
        """Full firmware picture for the CLI (`octacam flash`) and the GUI."""
        return self._fw.provisioning(
            plugin_name=self.name,
            device=self.device,
            firmware=self._firmware,
            firmware_ok=self._firmware_ok,
            extra={"auto_flash": self._auto_flash},
        )

    def flash_firmware(self, on_line: Callable[[str], None] | None = None) -> fw.FlashResult:
        """Compile + upload the current triggerbox firmware to the board.

        Delegates the close→upload→reopen→re-verify lifecycle to the shared
        FirmwareProvisioner (which holds the port lock so a concurrent reconnect
        can't seize the port, and refuses while a recording is live). Never raises."""
        result = self._fw.flash(on_line=on_line)
        if result.ok:
            self._arduino_state = "idle"  # the board rebooted after the upload
        else:
            # reopen() cleared _last_error; restore the failure so the GUI shows it.
            self._last_error = result.message
        self._broadcast_state()
        return result

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Headless (CLI) arm slice for ``octacam record`` — the configured spec."""
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
            "cameras": [c.to_dict() for c in self._cameras],
            "lights": [lt.to_dict() for lt in self._lights],
        }

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the Arduino when a triggerbox arm slice is present in params."""
        params = params or {}
        spec = params.get("triggerbox")
        if spec is None:
            spec = params.get("omniview")  # legacy plugin name, one-release shim
        if not isinstance(spec, dict):
            return
        if not self._link.is_open:
            log.warning(
                "triggerbox: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        if not self._firmware_ok:
            log.error(
                "triggerbox: firmware on %s (%r) is incompatible; refusing to arm — "
                "reflash arduino/triggerbox to TRIGGERBOX %d",
                self.device, self._firmware, _PROTOCOL_VERSION,
            )
            return

        fps = max(1, min(_MAX_FPS, _coerce_int(spec.get("fps"), self._default_fps)))
        duration_ms = max(1, min(0xFFFF_FFFF, _coerce_int(spec.get("duration_ms"), self._default_duration_ms)))
        cams = self._cameras_from_spec(spec)
        lights = self._lights_from_spec(spec)
        try:
            arm = self._build_arm_spec(fps, duration_ms, cams, lights)
            arm.to_bytes()  # validate the packet builds before we announce arming
        except Exception:
            log.exception("triggerbox: could not build arm packet; not arming")
            return

        log.info(
            "triggerbox: arming %d fps for %d ms — %d camera line(s), %d light channel(s)",
            fps, duration_ms, len(arm.cameras), len(arm.lights),
        )
        result = self._arm_and_wait(arm)
        # A write failure or missing ACK (but not an explicit reject) is the
        # signature of a wedged USB link — try one host bus reset + re-arm before
        # giving up, and make any remaining failure loud (GUI + log) so the
        # operator knows the recording will get no triggers.
        if result in ("write_failed", "timeout"):
            what = (
                "the serial write failed" if result == "write_failed"
                else f"no acknowledgement within {self._ack_timeout_s:.1f}s"
            )
            self._report_error(
                f"the board on {self.device} did not arm ({what}); external-"
                "triggered cameras will not be triggered. Attempting a USB reset…"
            )
            if self._recover_usb("the board stopped responding during arm"):
                log.info("triggerbox: re-arming %s after USB reset", self.device)
                result = self._arm_and_wait(arm)

        if result == "reject":
            reason = _REJECT_REASONS.get((self._last_reject or "")[:1], "unknown")
            self._report_error(
                f"{self.device} REJECTED the arm (code {self._last_reject!r}: {reason}); "
                "the board is not running — cameras will wait for a trigger that never fires"
            )
        elif result != "ok":
            self._report_error(
                f"the board on {self.device} still did not arm after a USB-reset "
                "attempt — external-triggered cameras will wait for a trigger that "
                "never fires. Power-cycle or replug the board and check the cable."
            )

    def _arm_and_wait(self, arm: ArmSpec) -> str:
        """Send an arm packet and classify the outcome.

        Returns ``"ok"`` (running ack seen, or ack-wait disabled), ``"reject"``
        (board sent E<code>), ``"timeout"`` (no ack in time), or ``"write_failed"``
        (the bytes never reached the OS — a wedged/closed link)."""
        self._armed_event.clear()
        self._last_reject = None
        if not self._link.send_arm(arm):
            return "write_failed"
        if self._ack_timeout_s <= 0:
            return "ok"
        if not self._armed_event.wait(self._ack_timeout_s):
            return "timeout"
        return "reject" if self._last_reject is not None else "ok"

    def on_recording_stop(self, aborted: bool) -> None:
        # Cancel on any end (abort, manual stop, clean finish); a cancel to an
        # already-idle board is a harmless no-op.
        self._link.send_cancel()
        self._set_arduino_state("idle")

    # -------------------------------------------------- web contributions

    def web_assets(self) -> Path:
        return Path(__file__).parent / "web"

    def api_router(self):
        from fastapi import APIRouter, Body

        router = APIRouter()

        @router.post("/api/triggerbox/reconnect")
        def reconnect(payload: dict = Body(default={})):
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self._configured_device = device.strip()
            error = self._open()
            check = self._fw_check
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

        @router.get("/api/triggerbox/status")
        def get_status():
            return {"ready": self._link.is_open, **self.status()}

        @router.get("/api/triggerbox/firmware")
        def get_firmware():
            """Firmware state vs. the sketch source + whether octacam can flash it."""
            return self.firmware_provisioning()

        @router.post("/api/triggerbox/flash")
        def flash(payload: dict = Body(default={})):
            """Compile + upload the current firmware, then report the new state.

            Runs synchronously (FastAPI dispatches this sync handler to a thread,
            so the event loop keeps serving); the whole compile+upload takes tens
            of seconds and the board reboots at the end."""
            result = self.flash_firmware()
            return {
                **result.to_dict(),
                "firmware": self._firmware,
                "firmware_ok": self._firmware_ok,
                "ready": self._link.is_open,
                "provisioning": self.firmware_provisioning(),
            }

        @router.get("/api/triggerbox/exposures")
        def get_exposures():
            """Live per-camera exposure timings for the tab's timing viz + auto-duty."""
            timings = self._camera_timings()
            return {
                "guard_us": self._strobe_guard_us,
                "duty_auto_default": self._default_duty_auto,
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
