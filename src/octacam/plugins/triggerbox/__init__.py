"""triggerbox rig trigger + light-controller plugin (opt-in).

Drives the Nano ESP32 running ``arduino/triggerbox`` on the EPFL
common-trigger-circuit board: camera-trigger lines plus three interchangeable
CCS light channels, each off, strobe, continuous or a pulse train on its own
clock. Pins are chosen at run time, so rewiring needs only the config::

    [[plugins]]
    name = "triggerbox"

    [plugins.options]
    device = "auto"            # a udev symlink, /dev/ttyACM0, COM3 or "auto"
    baud = 115200
    auto_flash = false         # headless: reflash a stale board without asking
    strobe_guard_us = 100      # added to the longest exposure by an auto duty
    cameras = [ { pin = "D13", pulse_us = 500 } ]
    lights = [
      { channel = 1, mode = "strobe", duty_mode = "auto" },
      { channel = 2, mode = "strobe", duty_mode = "manual", duty_percent = 20 },
      { channel = 3, mode = "off" },
    ]

Without ``cameras``/``lights`` it drives the classic rig: a D13 camera line and
channels 1 and 2 strobing at ``default_duty_percent``. An auto-duty strobe stays
on for ``max(TriggerDelay + ExposureTime) + strobe_guard_us`` over the live
cameras. Recordings use ``trigger_source = "managed"``.

Wire protocol v2, host -> board (little-endian):
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

import logging
import math
import struct
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from octacam import firmware as fw
from octacam.plugins.serial import SerialLink, SerialPlugin

log = logging.getLogger("octacam")

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


# ---- Finite trains (must model triggerbox.ino) ------------------------------


def period_us(fps: int) -> int:
    """The firmware's integer frame period for ``fps`` (triggerbox.ino's
    ``(1000000 + fps / 2) / fps``): what the board actually emits."""
    return max(2, (1_000_000 + fps // 2) // fps)


def pulse_count(fps: int, duration_ms: int) -> int:
    """Pulses a recording of ``duration_ms`` at ``fps`` asks for:
    ``round(fps * duration)``. The train the board can end on exactly may differ
    by a few above ~400 fps; :func:`plan_train` says what it emits."""
    return max(1, round(duration_ms * fps / 1000))


# prepare_outputs() clamps every camera line with the firmware's default and
# minimum pulse width (kDefaultCamPulseUs / kMinCamPulseUs).
_FW_DEFAULT_PULSE_US = 500
_FW_MIN_PULSE_US = 5
# A finite run goes idle once millis() - start >= duration_ms. Its millisecond
# boundaries fall anywhere against the frame clock's t0, so the idle comes
# anywhere in the duration's last millisecond. Around that: the run clock starts
# a µs or two before the frame clock (early), and the loop polls both every few
# µs, emitting an edge due in the same pass before it checks the run clock
# (late; this much also covers an interrupt).
RUN_END_EARLY_US = 5
RUN_END_LATE_US = 20
# How far a recording's pulse count may move from round(fps * duration) to a
# count the board can end on exactly (only above ~400 fps; see plan_train).
MAX_COUNT_SHIFT = 10


@dataclass(frozen=True)
class TrainPlan:
    """A finite train: the ``duration_ms`` to arm and the pulses the board emits.

    ``count`` pulses on every camera line. ``exact``: wherever in its last
    millisecond the run ends, every line gets exactly ``count`` complete pulses
    and no more. Otherwise the board's millisecond run clock cannot separate the
    last pulse from the next frame edge at this fps and ``count`` is the likeliest
    outcome: ``certain`` still guarantees it, the last pulse possibly cut short
    but to no less than half its width; without it the train may come out a
    pulse short or long. ``lights_cut`` lists ``(light index, µs)`` for each
    strobe whose last on-time the run's end may cut by up to that much, and
    ``light_overrun`` that a strobe of the frame after the train may start before
    the end.
    """

    duration_ms: int
    count: int
    exact: bool = True
    certain: bool = True
    lights_cut: tuple[tuple[int, int], ...] = ()
    light_overrun: bool = False

    def warnings(self, fps: int, lights) -> list[str]:
        """Where this train cannot end cleanly at *fps*, for the operator;
        ``lights`` are the arm's light records."""
        if not self.exact:
            # The lights end wherever the camera pulses let them.
            return [
                f"at {fps} fps the board's millisecond run clock cannot end the "
                "train cleanly after its last pulse (that needs about 1 ms between "
                "the last camera pulse's end and the next frame edge): "
                + (
                    "the last camera pulse may be cut short"
                    if self.certain
                    else "the last pulse may be lost, or one more may start, and the "
                    "recording then reports a missed pulse or an extra frame"
                )
            ]
        warnings = [
            f"the train's last strobe on {PIN_LABELS[lights[index][0]]} may end up "
            f"to {cut_us} µs early: it runs too close to the next frame edge for the "
            "run to end after it, so the last frame may be under-lit"
            for index, cut_us in self.lights_cut
        ]
        if self.light_overrun:
            warnings.append(
                "a strobe may flash once after the train: a camera line's delay "
                "keeps the run going past that strobe's next edge"
            )
        return warnings


def _camera_pulses(period: int, cameras) -> list[tuple[int, int]]:
    """``(rise, fall)`` µs into its frame of each camera line's pulse, clamped as
    triggerbox.ino's prepare_outputs() does. ``cameras`` are wire records
    ``(pin_id, pulse_us, delay_us)``. A train without a camera line still counts
    frames: a zero-width pulse on every frame edge."""
    pulses = []
    for _pin, pulse_us, delay_us in cameras:
        delay = min(int(delay_us), period - 1)
        pulse = max(int(pulse_us) or _FW_DEFAULT_PULSE_US, _FW_MIN_PULSE_US)
        pulse = min(pulse, period - 1 - delay if period > delay + 1 else 1)
        pulses.append((delay, delay + pulse))
    return pulses or [(0, 0)]


def _strobe_windows(period: int, lights) -> list[tuple[int, int, int]]:
    """``(light index, on, off)`` µs into its frame of each strobing light, as the
    firmware runs it: the delay clamped into the frame, the on-time cut at the
    frame's end. Only a strobe is frame-locked: a continuous light (and a strobe
    on for a whole period, which the firmware makes one) stays on for the run,
    and a pulse train keeps its own clock for its ``train_ms`` or the whole run,
    so the run's end stops those wherever they are."""
    windows = []
    for index, (_pin, mode, delay, on_us, _p2, _p3) in enumerate(lights):
        if mode == 1 and 0 < on_us < period:
            on = min(int(delay), period - 1)
            windows.append((index, on, min(on + int(on_us), period)))
    return windows


def _end_window(duration_ms: int) -> tuple[int, int]:
    """When a run of ``duration_ms`` may go idle: ``[first, last]`` µs from its
    first frame edge."""
    return (
        duration_ms * 1000 - 1000 - RUN_END_EARLY_US,
        duration_ms * 1000 + RUN_END_LATE_US,
    )


def _durations_ending_in(start: int, stop: int) -> tuple[int, int]:
    """The durations ``(lo, hi)`` (inclusive; none if lo > hi) whose run always
    ends in ``[start, stop)``, in µs from the train's first frame edge."""
    lo = -(-(start + 1000 + RUN_END_EARLY_US) // 1000)
    hi = (stop - 1 - RUN_END_LATE_US) // 1000
    return max(1, lo), hi


def plan_train(fps: int, pulses: int, cameras=(), lights=()) -> TrainPlan:
    """The run length to arm for a train of ``pulses``, and what the board emits.

    ``cameras`` / ``lights`` are the arm's wire records. The board raises camera
    line ``i`` at ``k * period + delay_i`` for frame ``k`` and goes idle
    (everything LOW) somewhere in the duration's last millisecond, so the run
    must end after every line's last pulse has fallen and before any line's next
    one rises. That needs about a millisecond between the two. From ~400 fps
    (with the default 500 µs pulse) a whole-millisecond end lands there for some
    counts only, and the nearest count within ``MAX_COUNT_SHIFT`` it lands for is
    taken instead. From ~650 fps (and at a few fps below it, 500 among them) it
    lands for none, and the plan is not ``exact``: it takes the count and
    duration whose end is least likely to lose or add a pulse, then least likely
    to cut one, preferring the wanted count.

    The count depends on the camera lines only, so ``trigger_train`` (which has
    no light timing: the auto strobe duty needs the live exposures) and the arm
    agree on it. The lights only choose among the durations that end that count
    exactly: the earliest that also lets every strobe's last on-time finish
    before the next frame edge, else the latest before that edge, so the last
    strobe loses as little as possible.
    """
    period = period_us(fps)
    cams = _camera_pulses(period, cameras)
    # µs into a frame by which its pulses have all risen, have all lasted half
    # their width (sure to trigger even if the end then cuts them) and have all
    # fallen, and at which the next frame's first pulse rises.
    all_rise = max(rise for rise, _ in cams)
    all_up = max(rise + (fall - rise + 1) // 2 for rise, fall in cams)
    all_down = max(fall for _, fall in cams)
    first_up = min(rise for rise, _ in cams)
    wanted = max(1, int(pulses))
    shifts = [0] + [s for i in range(1, MAX_COUNT_SHIFT + 1) for s in (i, -i)]
    ranks = {wanted + s: rank for rank, s in enumerate(shifts) if wanted + s >= 1}

    def ends(count: int, into_frame: int) -> tuple[int, int]:
        """End times (µs from the first frame edge) past ``into_frame`` of the
        train's last frame and before the next frame's first pulse."""
        return (count - 1) * period + into_frame, count * period + first_up

    for count in ranks:  # nearest first
        lo, hi = _durations_ending_in(*ends(count, all_down))
        if lo <= hi:
            return _place_train_end(period, count, lo, hi, lights)

    # No end fits any count near the wanted one. Take the duration whose likeliest
    # count is near it and least often comes out otherwise or with its last pulse
    # cut below half its width (sure to trigger above that), then the nearest
    # count, then the one that cuts the last pulse least.
    candidates: set[int] = set()
    for count in ranks:
        candidates.update(_durations_ending_in(*ends(count, all_up)))
        for into_frame in (all_rise, all_up, all_down):
            start, stop = ends(count, into_frame)
            centered = (start + stop) // 2 // 1000 + 1  # its end window's middle there
            candidates.update((centered - 1, centered, centered + 1))
    best: tuple[tuple[bool, int, int, int, int], int] | None = None
    for duration in candidates:
        if duration < 1:
            continue
        first, last = _end_window(duration)
        lowest = max(1, (first - first_up) // period)  # the counts it may end on
        highest = max(lowest, (last - all_rise) // period + 1)
        likeliest = max(
            range(lowest, highest + 1),
            key=lambda n: -_end_outside(duration, *ends(n, all_rise)),
        )
        key = (
            likeliest not in ranks,
            _end_outside(duration, *ends(likeliest, all_up)),
            ranks.get(likeliest, len(ranks)),
            _end_outside(duration, *ends(likeliest, all_down)),
            duration,
        )
        if best is None or key < best[0]:
            best = (key, likeliest)
    assert best is not None  # the wanted count's own centered ends are candidates
    (_far, unsure_us, _rank, _cut_us, duration), count = best
    plan = _place_train_end(period, count, duration, duration, lights)
    return replace(plan, exact=False, certain=unsure_us == 0)


def _end_outside(duration_ms: int, start: int, stop: int) -> int:
    """How many µs of a run's possible end times fall outside ``[start, stop)``."""
    first, last = _end_window(duration_ms)
    inside = min(last, stop - 1) - max(first, start)
    return (last - first) - max(0, inside)


def _place_train_end(period: int, count: int, lo: int, hi: int, lights) -> TrainPlan:
    """Pick the duration in ``[lo, hi]`` (all end ``count`` pulses alike) that
    suits the strobes best, and report what its end does to them."""
    last, following = (count - 1) * period, count * period
    strobes = _strobe_windows(period, lights)
    if not strobes:
        return TrainPlan(duration_ms=lo, count=count)
    # A strobe may lose the few µs by which a run can end early, immaterial to
    # its exposure: it counts as finished if it is by the nominal millisecond.
    strobes_off = last + max(off for _, _, off in strobes) - RUN_END_EARLY_US
    next_strobe = following + min(on for _, on, _ in strobes)
    clean_lo, clean_hi = _durations_ending_in(strobes_off, next_strobe)
    if max(lo, clean_lo) <= min(hi, clean_hi):
        duration = max(lo, clean_lo)
    else:
        # A strobe runs too close to the next frame edge to finish first: end
        # just before the edge (or the next strobe), so it loses the least.
        duration = max(lo, min(hi, clean_hi))
    earliest = duration * 1000 - 1000  # the nominal one (see above)
    return TrainPlan(
        duration_ms=duration,
        count=count,
        lights_cut=tuple(
            (index, min(off - on, last + off - earliest))
            for index, on, off in strobes
            if last + off > earliest
        ),
        light_overrun=_end_window(duration)[1] >= next_strobe,
    )


# ---- Plugin -----------------------------------------------------------------

DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 80
DEFAULT_DURATION_MS = 10_000
DEFAULT_CAM_PULSE_US = 0  # 0 → firmware default pulse width
DEFAULT_DUTY_AUTO = False
# Added to the longest TriggerDelay + ExposureTime by an auto-duty strobe, for
# trigger latency and jitter at the exposure's end (TriggerDelay covers its start).
DEFAULT_STROBE_GUARD_US = 100


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


def _camera_from_dict(d, default_pulse: int) -> CameraLine | None:
    if not isinstance(d, dict):
        log.warning("triggerbox: ignoring non-table camera entry %r", d)
        return None
    pin = str(d.get("pin", "D13")).upper()
    if pin not in PIN_LABELS:
        log.warning("triggerbox: unknown camera pin %r; using D13", pin)
        pin = "D13"
    elif pin in RESERVED_PINS:
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
    if mode not in LIGHT_MODE_IDS:
        log.warning("triggerbox: unknown light mode %r; using off", mode)
        mode = "off"
    mode = LIGHT_MODE_NAMES[LIGHT_MODE_IDS[mode]]  # canonicalize (pulse → pulse_train)
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


def _cameras_from(raw: list, default_pulse: int) -> list[CameraLine]:
    lines = (_camera_from_dict(entry, default_pulse) for entry in raw[:MAX_CAM])
    return [line for line in lines if line is not None]


def _lights_from(raw: list, default_duty_percent: float, default_duty_auto: bool) -> list[LightChannel]:
    lights = (
        _light_from_dict(entry, default_duty_percent, default_duty_auto)
        for entry in raw[:MAX_LIGHT]
    )
    return [light for light in lights if light is not None]


class TriggerboxPlugin(SerialPlugin):
    """Arms the board with the fps, duration and every camera line and light
    channel; the board then runs them on its own clock."""

    name = "triggerbox"
    generates_trigger = True
    web_dir = Path(__file__).parent / "web"
    firmware = fw.FirmwareSpec(
        name="triggerbox",
        sketch_dir=fw.resolve_sketch_dir("triggerbox"),
        fqbn="arduino:esp32:nano_nora",
        banner_prefix=BANNER,
        protocol_version=PROTOCOL_VERSION,
        build_define="TRIGGERBOX_FW_BUILD",
    )
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/triggerbox/reconnect"
    state_topic = "triggerbox_state"
    recover_silent = True
    _link: TriggerboxLink

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
        super().__init__(
            TriggerboxLink(self._on_state, self._on_link_broken),
            device=device,
            baud=baud,
            auto_flash=bool(auto_flash),
        )
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._default_duty_percent = default_duty_percent
        self._default_duty_auto = default_duty_auto
        self._strobe_guard_us = max(0, strobe_guard_us)
        self._default_cam_pulse_us = default_cam_pulse_us
        self._cameras: list[CameraLine] = cameras or [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]
        self._lights: list[LightChannel] = lights or []
        # Tab edits replace _cameras/_lights; snapshot_options compares with these.
        self._configured_cameras = [replace(c) for c in self._cameras]
        self._configured_lights = [replace(lt) for lt in self._lights]
        # A tab edit re-arms the board only during an (indefinite) preview arm.
        self._preview_armed = False

    @classmethod
    def from_options(cls, options: dict) -> TriggerboxPlugin:
        def _opt_int(key: str, default: int) -> int:
            try:
                return int(options.get(key, default))
            except (TypeError, ValueError):
                log.warning("triggerbox plugin: invalid %s %r; using %d", key, options.get(key), default)
                return default

        def _opt_float(*keys: str, default: float) -> float:
            # The first key present: configs spell some options two ways.
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
        raw_cams = options.get("cameras")
        if raw_cams is not None and not isinstance(raw_cams, list):
            log.warning("triggerbox plugin: 'cameras' must be an array of tables; ignoring %r", raw_cams)
        cameras = _cameras_from(raw_cams, default_cam_pulse_us) if isinstance(raw_cams, list) else []
        if not cameras:
            cameras = [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]

        # Light channels: explicit array, else the classic ch1+ch2 strobe.
        raw_lights = options.get("lights")
        if isinstance(raw_lights, list):
            lights = _lights_from(raw_lights, default_duty_percent, default_duty_auto)
        elif raw_lights is None:
            dm = "auto" if default_duty_auto else "manual"
            lights = [
                LightChannel(channel=1, pin="D5", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
                LightChannel(channel=2, pin="D6", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
            ]
        else:
            log.warning("triggerbox plugin: 'lights' must be an array of tables; ignoring %r", raw_lights)
            lights = []

        return cls(
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

    def busy_reason(self) -> str | None:
        # The controller's state comes first: it flips before the board's 'R' arrives.
        controller = self.controller
        if controller is not None and controller.recording_active:
            return "refusing to flash while a recording is active — stop it first"
        if self.board_state == "running":
            return "refusing to flash while the board is armed/running — stop the recording first"
        return None

    def _on_state(self, token: str) -> None:
        state = {"R": "running", "D": "done", "C": "idle"}[token]
        if state == "running":
            self.last_error = None  # an arm took
        self._set_state(state)

    def _on_link_broken(self) -> None:
        self._set_state("idle")

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self.board_state = "idle"

    def status(self) -> dict:
        return {
            **super().status(),
            "guard_us": self._strobe_guard_us,
            "cameras": [asdict(c) for c in self._cameras],
            "lights": [asdict(lt) for lt in self._lights],
        }

    # -------------------------------------------------- spec -> arm packet

    def _camera_windows(self) -> list[tuple[str, float, float | None]]:
        """``(name, trigger delay, exposure)`` µs of each live camera (the
        controller's), for the auto strobe duty."""
        if self.controller is None:
            return []
        return [
            (camera.name or f"cam{index}", *camera.trigger_window_us())
            for index, camera in enumerate(self.controller.camera_system)
        ]

    def _auto_led_on_us(self) -> float | None:
        """The strobe on-time covering the longest exposure plus the guard, or
        None when no exposure can be read."""
        coverages = [
            delay + exposure
            for _name, delay, exposure in self._camera_windows()
            if exposure is not None
        ]
        if not coverages:
            return None
        return max(coverages) + self._strobe_guard_us

    def _cameras_from_spec(self, spec: dict) -> list[CameraLine]:
        raw = spec.get("cameras")
        if isinstance(raw, list):
            return _cameras_from(raw, self._default_cam_pulse_us)
        return [replace(c) for c in self._cameras]

    def _lights_from_spec(self, spec: dict) -> list[LightChannel]:
        raw = spec.get("lights")
        if isinstance(raw, list):
            return _lights_from(raw, self._default_duty_percent, self._default_duty_auto)
        return [replace(lt) for lt in self._lights]

    def _spec_duration_ms(self, spec: dict) -> int:
        return max(
            1,
            min(0xFFFF_FFFF, _coerce_int(spec.get("duration_ms"), self._default_duration_ms)),
        )

    def _resolve_arm(
        self,
        spec: dict,
        lights: Sequence[LightChannel] = (),
        auto_led_on_us: float | None = None,
    ) -> ArmSpec:
        """The packet a start slice arms, running until cancelled (duration 0):
        its fps and camera lines, and *lights* with an auto strobe on for
        *auto_led_on_us* (its manual duty when None). Pure: no camera reads."""
        fps = max(1, min(MAX_FPS, _coerce_int(spec.get("fps"), self._default_fps)))
        return ArmSpec(
            fps=fps,
            duration_ms=0,
            cameras=[c.record() for c in self._cameras_from_spec(spec)[:MAX_CAM]],
            lights=[lt.resolve(1_000_000.0 / fps, auto_led_on_us) for lt in lights[:MAX_LIGHT]],
        )

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """The configured spec as the arm slice for headless ``octacam record``."""
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
            "cameras": [asdict(c) for c in self._cameras],
            "lights": [asdict(lt) for lt in self._lights],
        }

    def snapshot_options(self, params: dict | None) -> dict | None:
        """The camera lines and light channels a recording armed, as config
        options; None when it armed none or what the config says. Off channels
        are left out on both sides: the tab never sends them, and an empty
        ``lights`` list reloads as all-off."""
        if params is None:
            return None
        cameras = self._cameras_from_spec(params)
        lights = [lt for lt in self._lights_from_spec(params) if lt.mode != "off"]
        configured = [lt for lt in self._configured_lights if lt.mode != "off"]
        if cameras == self._configured_cameras and lights == configured:
            return None
        return {
            "cameras": [asdict(c) for c in cameras],
            "lights": [asdict(lt) for lt in lights],
        }

    def trigger_train(self, params: dict | None) -> dict | None:
        """The exact period and pulse count on_recording_start emits for
        *params*. The count depends on the camera lines only: the lights' auto
        duty needs a camera read, which this pure hook (called under the
        controller lock) must not make."""
        if params is None:
            return None
        arm = self._resolve_arm(params)
        wanted = pulse_count(arm.fps, self._spec_duration_ms(params))
        plan = plan_train(arm.fps, wanted, arm.cameras)
        return {"period_ns": period_us(arm.fps) * 1000, "count": plan.count}

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Emit *pulses* sacrificial pulses on the camera lines, lights dark (see
        "Priming" in CLAUDE.md). Returns once the board reports the burst done,
        or once it is cancelled, so the train starts on a fresh clock."""
        if params is None or not self._link.is_open or not self.firmware_ok:
            return False
        try:
            arm = self._resolve_arm(params)
            if not arm.cameras or pulses <= 0:
                return False
            arm = replace(arm, duration_ms=plan_train(arm.fps, pulses, arm.cameras).duration_ms)
        except Exception:
            log.exception("triggerbox: could not build the priming packet")
            return False
        if self._link.arm(arm) != "ok":
            return False
        if not self._link.wait_done(arm.duration_ms / 1000 + ACK_TIMEOUT_S):
            log.warning(
                "triggerbox: no end-of-run from %s after the priming pulses; "
                "cancelling", self.device,
            )
            self._cancel()
        self._set_state("idle")
        return True

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the board when the start request holds a triggerbox slice."""
        self._preview_armed = False
        if params is not None:
            self._arm(params, recording=True)

    def on_recording_stop(self, aborted: bool) -> None:
        self._link.send_cancel()  # on every end; an idle board ignores it
        self._set_state("idle")

    def on_preview_start(self, params: dict | None) -> None:
        """Arm the board until cancelled, with the recording's spec."""
        if params is None:
            return
        self._preview_armed = True
        self._arm(params, recording=False)

    def on_preview_stop(self) -> None:
        # Waits for the 'C': a recording's record grab starts next and must not
        # see a stray preview pulse.
        self._preview_armed = False
        self._cancel()
        self._set_state("idle")

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """Adopt a camera/light edit the tab pushes, so the preview arm and the
        timing follow the tab. A running preview is re-armed with it: a same-fps
        re-arm keeps the board's frame clock and takes effect at the next edge,
        so the exposing cameras never see a stray trigger."""
        if not isinstance(message, dict) or message.get("type") != "triggerbox_spec":
            return False
        spec = message.get("spec")
        if not isinstance(spec, dict):
            return True  # ours, but malformed
        new_cams = self._cameras_from_spec(spec)
        new_lights = self._lights_from_spec(spec)
        changed = new_cams != self._cameras or new_lights != self._lights
        self._cameras = new_cams
        self._lights = new_lights
        # The tab also pushes on redraws: re-arm only on a real change.
        if changed and self._preview_armed and self._link.is_open:
            self._arm(spec, recording=False)
        return True

    # -------------------------------------------------- arming

    def _arm(self, spec: dict, *, recording: bool) -> None:
        """Arm the board from a slice: a recording plans its finite train from
        the slice, a preview runs until cancelled with the same packet otherwise,
        so it strobes as the recording will."""
        subject = "external-triggered cameras" if recording else "preview"
        if not self._link.is_open:
            self.report_error(
                f"the board on {self.device} is not connected; {subject} will not "
                "be triggered (cameras wait for a trigger that never fires). "
                "Check the cable / reconnect the board."
            )
            return
        if not self.firmware_ok:
            self.report_error(
                f"the firmware on {self.device} ({self.banner!r}) is incompatible; "
                f"{subject} will not be triggered. Reflash arduino/triggerbox to "
                f"TRIGGERBOX {PROTOCOL_VERSION}."
            )
            return
        try:
            lights = self._lights_from_spec(spec)
            auto_led_on_us = None
            if any(lt.wants_auto() for lt in lights):
                auto_led_on_us = self._auto_led_on_us()
                if auto_led_on_us is None:
                    log.warning(
                        "triggerbox: auto strobe duty requested but no camera exposure "
                        "could be read; falling back to the manual duty percent"
                    )
            arm = self._resolve_arm(spec, lights, auto_led_on_us)
            if recording:  # the train trigger_train counts
                wanted = pulse_count(arm.fps, self._spec_duration_ms(spec))
                plan = plan_train(arm.fps, wanted, arm.cameras, arm.lights)
                arm = replace(arm, duration_ms=min(0xFFFF_FFFF, plan.duration_ms))
                if plan.count != wanted:
                    log.info(
                        "triggerbox: at %d fps the board's millisecond run clock "
                        "cannot end a train cleanly after pulse %d; arming %d pulses "
                        "instead (the recording counts %d)",
                        arm.fps, wanted, plan.count, plan.count,
                    )
                for warning in plan.warnings(arm.fps, arm.lights):
                    log.warning("triggerbox: %s", warning)
                what = f"{plan.count} pulses ({arm.duration_ms} ms)"
            else:
                what = "preview (until cancel)"
            arm.to_bytes()  # it must pack before arming is announced
        except Exception:
            log.exception("triggerbox: could not build arm packet; not arming")
            return
        log.info(
            "triggerbox: arming %d fps for %s — %d camera line(s), %d light channel(s)",
            arm.fps, what, len(arm.cameras), len(arm.lights),
        )
        self._send_arm(arm, subject)

    def _send_arm(self, arm: ArmSpec, subject: str) -> None:
        """Arm and report any failure. A failed write or a missing ack, never a
        reject, is a wedged USB link: one bus reset and re-arm."""
        result = self._link.arm(arm)
        if result in ("write_failed", "timeout"):
            what = (
                "the serial write failed" if result == "write_failed"
                else f"no acknowledgement within {ACK_TIMEOUT_S:.1f}s"
            )
            self.report_error(
                f"the board on {self.device} did not arm ({what}); {subject} "
                "will not be triggered. Attempting a USB reset…"
            )
            if self._recover_usb("the board stopped responding during arm"):
                log.info("triggerbox: re-arming %s after USB reset", self.device)
                result = self._link.arm(arm)
        if result == "reject":
            code = self._link.reject
            reason = REJECT_REASONS.get((code or "")[:1], "unknown")
            self.report_error(
                f"{self.device} REJECTED the arm (code {code!r}: {reason}); "
                "the board is not running — cameras will wait for a trigger that never fires"
            )
        elif result != "ok":
            self.report_error(
                f"the board on {self.device} still did not arm after a USB-reset "
                f"attempt — {subject} will wait for a trigger that never fires. "
                "Power-cycle or replug the board and check the cable."
            )

    def _cancel(self) -> None:
        if not self._link.cancel():
            log.warning("triggerbox: %s did not acknowledge the cancel", self.device)

    # -------------------------------------------------- web contributions

    def api_router(self):
        router = super().api_router()

        @router.get("/api/triggerbox/exposures")
        def get_exposures():
            """Live per-camera exposure timings for the tab's timing plot."""
            return {
                "guard_us": self._strobe_guard_us,
                "duty_auto_default": self._default_duty_auto,
                "cameras": [
                    {
                        "index": index,
                        "name": name,
                        "exposure_us": exposure,
                        "trigger_delay_us": delay,
                    }
                    for index, (name, delay, exposure) in enumerate(self._camera_windows())
                ],
            }

        return router
