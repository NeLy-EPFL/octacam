"""The triggerbox wire protocol v2 (must match ``arduino/triggerbox``) and the
serial link that speaks it.

Host -> board (little-endian):
  [0xA5][version u8 = 2][payload_len u16][payload][xor u8]  arm
  [0xCA]                                                    cancel
  [0x3F] '?'                                                identify
  payload = fps u16 | duration_ms u32 | n_cam u8 | n_light u8
            | n_cam x (pin_id u8, pulse_us u16, delay_us u16)
            | n_light x (pin_id u8, mode u8, p0 u32, p1 u32, p2 u32, p3 u32)
Board -> host, newline-terminated: "R" running, "D" done, "C" cancelled or
idle, "E<c>" rejected, "TRIGGERBOX <version> <build>" the identify reply.
"""

from __future__ import annotations

import math
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass

from octacam.plugins.serial import SerialLink

PROTOCOL_VERSION = 2
BANNER = "TRIGGERBOX"
MAX_FPS = 5000
MAX_CAM = 12
MAX_LIGHT = 3
DEFAULT_DUTY_PERCENT = 20.0

# How long an arm waits for the board's 'R' (or 'E').
ACK_TIMEOUT_S = 1.0
# How long a cancel waits for 'C'. It must land before a recording's record grab
# starts, or a last preview pulse reaches a camera that is already counting.
CANCEL_ACK_TIMEOUT_S = 0.3

_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
_HDR = struct.Struct("<BBH")      # magic u8, version u8, payload_len u16
_FIXED = struct.Struct("<HIBB")   # fps u16, duration_ms u32, n_cam u8, n_light u8
_CAM = struct.Struct("<BHH")      # pin_id u8, pulse_us u16, delay_us u16
_LIGHT = struct.Struct("<BBIIII")  # pin_id u8, mode u8, p0 u32, p1 u32, p2 u32, p3 u32

# The wire's pin ids (by index): the single source of truth, mirrored by kPinTable
# in triggerbox.ino (a test keeps them equal). D2/D3/D4 drive the status LED and
# the firmware rejects them as outputs.
PIN_LABELS = (
    "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11",
    "D12", "D13", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
)
RESERVED_PINS = frozenset({"D2", "D3", "D4"})
# CCS light channel (1/2/3) → default board pin.
LIGHT_PIN_BY_CHANNEL = {1: "D5", 2: "D6", 3: "D7"}

# Light modes (name ↔ wire value). "pulse" is accepted as an alias of pulse_train.
LIGHT_MODE_IDS = {"off": 0, "strobe": 1, "continuous": 2, "pulse_train": 3, "pulse": 3}
LIGHT_MODE_NAMES = {0: "off", 1: "strobe", 2: "continuous", 3: "pulse_train"}

# The firmware's 'E<c>' reject codes.
REJECT_REASONS = {
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


def _u16(v) -> int:
    return max(0, min(0xFFFF, int(v)))


def _u32(v) -> int:
    return max(0, min(0xFFFF_FFFF, int(v)))


def pin_id(label: str) -> int:
    """Index of a pin label in PIN_LABELS. Raises ValueError if unknown."""
    return PIN_LABELS.index(str(label).upper())


@dataclass
class CameraLine:
    """One camera-trigger output line."""

    pin: str = "D13"
    pulse_us: int = 0  # 0 → firmware default (500 µs)
    delay_us: int = 0

    def record(self) -> tuple[int, int, int]:
        return (pin_id(self.pin), _u16(self.pulse_us), _u16(self.delay_us))


@dataclass
class LightChannel:
    """One CCS light channel; ``channel`` only picks the default pin."""

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
        """The wire record ``(pin_id, mode, p0, p1, p2, p3)``. An auto strobe
        takes ``auto_led_on_us``, or its manual duty when that is None."""
        pid = pin_id(self.pin)
        mode = LIGHT_MODE_IDS.get(self.mode, 0)
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


@dataclass
class ArmSpec:
    """An arm packet: fps, duration and the outputs' wire records."""

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
        body = bytes([PROTOCOL_VERSION]) + struct.pack("<H", len(payload)) + payload
        checksum = 0
        for byte in body:
            checksum ^= byte
        return _HDR.pack(_ARM_MAGIC, PROTOCOL_VERSION, len(payload)) + payload + bytes([checksum])


class TriggerboxLink(SerialLink):
    """The serial link to the triggerbox. It owns the board's answers: arms and
    cancels are serialized, ack wait included, so concurrent ones (a preview
    re-arm and a recording arm) cannot take each other's answer."""

    name = "triggerbox"
    banner_prefix = BANNER

    def __init__(self, on_state: Callable[[str], None], on_broken: Callable[[], None]):
        super().__init__(on_broken)
        self._on_state = on_state  # 'R', 'D' or 'C', after its event is set
        self._arm_lock = threading.Lock()
        self._answered = threading.Event()  # 'R' or 'E<c>' to the last arm
        self._done = threading.Event()  # 'D': the last arm's run is over
        self._idle = threading.Event()  # 'C' to the last cancel
        self._events = {"R": self._answered, "D": self._done, "C": self._idle}
        self.reject: str | None = None  # the last arm's reject code

    def arm(self, spec: ArmSpec) -> str:
        """Send an arm: ``"ok"`` ('R'), ``"reject"`` ('E<c>', code in
        :attr:`reject`), ``"timeout"`` or ``"write_failed"`` (a wedged or closed link)."""
        with self._arm_lock:
            self._answered.clear()
            self._done.clear()
            self.reject = None
            if not self._write(spec.to_bytes()):
                return "write_failed"
            if not self._answered.wait(ACK_TIMEOUT_S):
                return "timeout"
            return "reject" if self.reject is not None else "ok"

    def cancel(self) -> bool:
        """Cancel the run and wait (bounded) for the board's 'C'; True if it
        answered or the link is closed."""
        with self._arm_lock:
            self._idle.clear()
            self.send_cancel()
            if not self.is_open:
                return True
            return self._idle.wait(min(ACK_TIMEOUT_S, CANCEL_ACK_TIMEOUT_S))

    def send_cancel(self) -> None:
        """Cancel without waiting; an idle board ignores it."""
        self._write(bytes([_CANCEL_MAGIC]))

    def wait_done(self, timeout_s: float) -> bool:
        """Wait for the end of the run the last arm started."""
        return self._done.wait(timeout_s)

    def _feed(self, chunk: bytes) -> None:
        for token in self._lines(chunk):
            if self._identified(token):
                continue
            event = self._events.get(token)
            if event is not None:
                event.set()
                self._dispatch(self._on_state, token)
            elif token[0] == "E":
                self.reject = token[1:]
                self._answered.set()
