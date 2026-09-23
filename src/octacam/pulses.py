"""Trigger-pulse accounting: which trigger pulse exposed each delivered frame.

A hardware-triggered camera that misses a pulse delivers no frame for it, and
nothing in the frame stream says so: the next frame simply arrives one period
late. Counting frames therefore cannot tell a camera that captured every pulse
from one that missed some and caught up on trailing pulses — which is exactly how
a desynchronized rig used to look healthy. This module recovers the pulse index
of every frame from the camera's own hardware timestamps, so a miss is detected
(and can be filled in the video) at the moment it happens.

:class:`PulseTracker` is the online form, fed one frame at a time by the record
loop; :func:`analyze_timestamps` runs the same tracker over a saved timestamp
series for ``octacam check``. Each interval between two delivered frames is
rounded to a whole number of pulse periods. That is unambiguous because a camera
never exposes *before* its pulse and the delay after it — trigger latency, a
falling-edge trigger (~500 µs), readout-limited catch-up after a late frame, a
late pulse from the trigger source — stays far below half a period; so, unlike a
phase-locked model, it follows the trigger source through a phase jump (a
re-armed board) without losing count. The period used is the camera clock's
*measured* one, so ppm drift between the camera and the trigger source cannot
accumulate across a long outage.

Pure logic, no I/O: everything is unit-testable without hardware.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# A FLIR Grasshopper3's 64-bit timestamp is extended from a counter whose seconds
# field wraps every 128 s; a frame that lands within ~60 µs of a wrap can come out
# exactly 128 s ahead (seen in a real recording: an interval of 128.008 s where the
# frame was 8 ms after its predecessor). A jump by a multiple of this is folded
# back instead of being read as 16 000 missed pulses.
TIMESTAMP_WRAP_NS = 128_000_000_000

# A frame whose exposure is this much later than its pulse's expected time is
# reported as late. The GS3 falling-edge failure mode is ~500 µs late; a camera
# catching up at its readout limit after a late frame is ~200 µs late, which is
# the normal overlap-readout recovery and is not flagged.
LATE_THRESHOLD_NS = 250_000

# Single-period intervals within this fraction of the period refine the measured
# period; anything noisier (late triggers, board jitter) is counted but never
# steers it.
_CLEAN_FRACTION = 1 / 16
# Weight of each clean interval in the measured period (an exponential average
# over ~this many intervals: a few µs of jitter averages to well under 1 ppm).
_PERIOD_WINDOW = 256
# The measured period may deviate from the nominal one by this fraction before
# the camera is reported as not following the trigger clock (a camera free-
# running at its readout limit, e.g. under a pulse that outlasts its exposure).
CLOCK_MISMATCH_FRACTION = 0.01
# A camera-time interval may differ from the host-time interval between the same
# two deliveries by this much before the camera clock is treated as having
# jumped (queued frames make host deliveries bunch up, but never by seconds).
_GLITCH_TOLERANCE_NS = 1_500_000_000


@dataclass(frozen=True)
class PulseClock:
    """The trigger train a recording is clocked by.

    ``period_ns`` is the exact pulse period the trigger source emits (for the
    triggerbox, its integer-microsecond period), and ``count`` the number of
    pulses in the train, or None when it is not known in advance (an external
    master octacam does not drive). ``source`` says who drives it
    (``"managed"``, ``"software"`` or ``"external"``); ``fill`` whether a missed
    pulse is filled in the video (octacam-driven trains only — an external clock
    octacam cannot see may be irregular by design, so it is only reported).
    """

    period_ns: int
    count: int | None = None
    source: str = "managed"
    fill: bool = True

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "period_ns": self.period_ns,
            "fps": round(1e9 / self.period_ns, 6) if self.period_ns else None,
            "count": self.count,
            "fill": self.fill,
        }


@dataclass
class Assignment:
    """Where one delivered frame landed.

    ``pulse`` is its pulse index (None for an ``extra`` frame, which belongs to
    no pulse of the train and is discarded); ``missed`` the pulses skipped
    since the previous frame; ``residual_ns`` how much later this exposure
    trails its pulse than the previous frame trailed its own (the interval's
    excursion from a whole number of periods), and ``late_ns`` that excursion
    when it exceeds the late threshold (both 0 when the index came from a
    trigger sequence rather than a timestamp).
    """

    pulse: int | None
    missed: range = range(0)
    late_ns: int = 0
    residual_ns: int = 0

    @property
    def extra(self) -> bool:
        return self.pulse is None


@dataclass
class _Glitch:
    pulse: int
    kind: str  # "wrap" (folded a 128 s jump) | "reanchor" (unexplained jump)
    jump_ns: int


@dataclass
class PulseTracker:
    """Assign each delivered frame of one camera to a pulse of ``clock``.

    Feed frames in delivery order through :meth:`assign` (hardware timestamp,
    plus the host arrival time when known), or :meth:`assign_index` when the
    source reports the pulse index itself (the software-trigger hand-off). The
    first frame is pulse ``first_pulse`` (0: the recording controller primes the
    cameras so none of them loses the train's first pulses).
    """

    clock: PulseClock
    first_pulse: int = 0
    late_threshold_ns: int = LATE_THRESHOLD_NS
    missed: list[int] = field(default_factory=list)
    late: list[int] = field(default_factory=list)
    glitches: list[_Glitch] = field(default_factory=list)
    extra: int = 0
    last_pulse: int | None = None

    def __post_init__(self) -> None:
        self._nominal = float(self.clock.period_ns)
        self._period = self._nominal
        self._clean_intervals = 0
        self._last_ts: int | None = None
        self._last_host: int | None = None
        self._offset = 0  # folded clock correction added to every timestamp

    # ------------------------------------------------------------- queries

    @property
    def next_pulse(self) -> int:
        """The pulse the next frame is expected to belong to."""
        return self.first_pulse if self.last_pulse is None else self.last_pulse + 1

    @property
    def complete(self) -> bool:
        """True once the last pulse of a counted train has been assigned."""
        count = self.clock.count
        return count is not None and self.next_pulse >= count

    @property
    def period_ns(self) -> float:
        """The measured pulse period, in camera-clock nanoseconds."""
        return self._period

    @property
    def clock_mismatch(self) -> bool:
        """True when the camera's frames do not follow the trigger clock's period.

        Only decided once enough clean intervals were seen; a camera exposing
        faster than its trigger (free-running at its readout limit) still maps
        one frame per interval, so this is the check that exposes it."""
        return self._clean_intervals >= 32 and (
            abs(self._period - self._nominal) > CLOCK_MISMATCH_FRACTION * self._nominal
        )

    def expected_ts(self, pulse: int) -> int | None:
        """The camera timestamp pulse ``pulse`` is expected at (None before the
        first frame). Used to time-stamp a filled frame."""
        if self._last_ts is None or self.last_pulse is None:
            return None
        return int(round(self._last_ts + (pulse - self.last_pulse) * self._period
                         - self._offset))

    # ---------------------------------------------------------- assignment

    def assign_index(self, pulse: int) -> Assignment:
        """Record a frame whose pulse index the trigger source reported."""
        if pulse < self.next_pulse or (
            self.clock.count is not None and pulse >= self.clock.count
        ):
            self.extra += 1
            return Assignment(None)
        missed = range(self.next_pulse, pulse)
        self.missed.extend(missed)
        self.last_pulse = pulse
        return Assignment(pulse, missed)

    def assign(self, timestamp_ns: int, host_ns: int | None = None) -> Assignment:
        """Assign a frame from its camera timestamp (and host arrival time)."""
        if self.last_pulse is None or self._last_ts is None:
            self.last_pulse = self.first_pulse
            self._last_ts = timestamp_ns
            self._last_host = host_ns
            return Assignment(self.first_pulse)
        ts = timestamp_ns + self._offset
        dt = ts - self._last_ts
        if dt == 0:
            self.extra += 1  # the same exposure delivered twice
            return Assignment(None)
        dh = (
            host_ns - self._last_host
            if host_ns is not None and self._last_host is not None
            else None
        )
        ts = self._correct(ts, dt, dh)
        dt = ts - self._last_ts
        steps = round(dt / self._period)
        pulse = self.last_pulse + steps
        if steps <= 0 or (self.clock.count is not None and pulse >= self.clock.count):
            # Less than half a period after the previous frame (a spurious
            # re-trigger, e.g. a pulse outlasting the exposure: the first frame of
            # a pulse wins), or beyond the end of the train (a runt or a stray).
            self.extra += 1
            return Assignment(None)
        # How much later this exposure trails its pulse than the previous one
        # did: positive for a late trigger, negative while catching up after one.
        excursion = int(round(dt - steps * self._period))
        missed = range(self.last_pulse + 1, pulse)
        self.missed.extend(missed)
        late = excursion if excursion > self.late_threshold_ns else 0
        if late:
            self.late.append(pulse)
        if steps == 1 and abs(excursion) < self._nominal * _CLEAN_FRACTION:
            self._clean_intervals += 1
            weight = 1.0 / min(self._clean_intervals, _PERIOD_WINDOW)
            self._period += weight * (dt - self._period)
        self.last_pulse = pulse
        self._last_ts = ts
        if host_ns is not None:
            self._last_host = host_ns
        return Assignment(pulse, missed, late, excursion)

    # ----------------------------------------------------------- internals

    def _looks_like_frame_interval(self, dt: int, dh: int | None) -> bool:
        if dt <= 0:
            return False
        if dh is not None:
            return abs(dt - dh) <= _GLITCH_TOLERANCE_NS
        # Offline (no host times): any positive interval is taken at face value
        # (a real outage is a run of missed pulses) — except one that sits a
        # whole timestamp wrap away from a normal frame interval.
        return not self._near_wrap(dt)

    def _near_wrap(self, dt: int) -> bool:
        return any(
            abs(dt - k * TIMESTAMP_WRAP_NS) < 16 * self._period for k in (1, 2)
        )

    def _correct(self, ts: int, dt: int, dh: int | None) -> int:
        """Undo a camera-clock jump between two frames; returns the timestamp to use.

        A jump by a whole timestamp wrap is folded back; any other jump the host
        time contradicts re-anchors the camera timeline, placing the frame by
        host time (offline: as the next pulse). Both are recorded in
        :attr:`glitches`.
        """
        if self._looks_like_frame_interval(dt, dh):
            return ts
        for wraps in (1, -1, 2, -2):
            folded = dt - wraps * TIMESTAMP_WRAP_NS
            if folded <= 0:
                continue
            if dh is not None:
                if abs(folded - dh) > _GLITCH_TOLERANCE_NS:
                    continue
            elif folded >= 16 * self._period:
                continue
            self._offset -= wraps * TIMESTAMP_WRAP_NS
            self.glitches.append(
                _Glitch(self.next_pulse, "wrap", wraps * TIMESTAMP_WRAP_NS)
            )
            return ts - wraps * TIMESTAMP_WRAP_NS
        steps = max(1, round(dh / self._period)) if dh is not None else 1
        assert self._last_ts is not None
        target = self._last_ts + steps * self._period
        correction = int(round(target - ts))
        self._offset += correction
        self.glitches.append(_Glitch(self.next_pulse, "reanchor", -correction))
        return ts + correction


# ---------------------------------------------------------------- offline


@dataclass
class TimestampReport:
    """What a saved per-frame timestamp series says about one camera."""

    frames: int
    period_ns: float
    pulses: list[int]  # pulse index of every frame (first frame = 0)
    missed: list[int]
    late: list[int]
    extra: int
    glitches: list[tuple[int, str, int]]
    residual_ns: list[int]  # per frame: interval excursion (see Assignment)
    clock_mismatch: bool = False

    @property
    def span(self) -> int:
        """Pulses from the first to the last frame, inclusive."""
        return (self.pulses[-1] + 1) if self.pulses else 0


def nominal_period_ns(timestamps, fps: float | None = None) -> float | None:
    """The period to track: ``1e9/fps`` when given, else the median interval."""
    if fps:
        return 1e9 / fps
    if len(timestamps) < 3:
        return None
    diffs = sorted(b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a)
    if not diffs:
        return None
    return float(diffs[len(diffs) // 2])


def analyze_timestamps(
    timestamps, fps: float | None = None, *, late_threshold_ns: int = LATE_THRESHOLD_NS
) -> TimestampReport:
    """Run the pulse tracker over one camera's saved hardware timestamps.

    ``fps`` is the recording's target rate (recording_summary.json); without it
    the median interval stands in. Frames the tracker would discard as extra are
    kept in ``pulses`` with the previous frame's index, so the lists stay
    parallel to the input.
    """
    ts = [int(t) for t in timestamps]
    period = nominal_period_ns(ts, fps)
    if not ts or period is None:
        return TimestampReport(len(ts), period or math.nan, list(range(len(ts))), [], [], 0, [], [0] * len(ts))
    tracker = PulseTracker(PulseClock(int(round(period)), None, "external", fill=False),
                           late_threshold_ns=late_threshold_ns)
    pulses: list[int] = []
    residuals: list[int] = []
    for t in ts:
        a = tracker.assign(t)
        pulses.append(a.pulse if a.pulse is not None else (pulses[-1] if pulses else 0))
        residuals.append(a.residual_ns)
    return TimestampReport(
        frames=len(ts),
        period_ns=tracker.period_ns,
        pulses=pulses,
        missed=list(tracker.missed),
        late=list(tracker.late),
        extra=tracker.extra,
        glitches=[(g.pulse, g.kind, g.jump_ns) for g in tracker.glitches],
        residual_ns=residuals,
        clock_mismatch=tracker.clock_mismatch,
    )


def timing_events(report: TimestampReport, threshold_ns: int = 100_000) -> dict[int, int]:
    """Pulses whose interval deviated from the clock by more than ``threshold_ns``.

    The trigger source's own jitter (a late pulse, the start of a train) shows up
    at the same pulse in every camera it drives, so these events let two cameras'
    frame indices be lined up after the fact (see :func:`estimate_offset`).
    """
    out: dict[int, int] = {}
    for pulse, r in zip(report.pulses, report.residual_ns, strict=True):
        if abs(r) > threshold_ns:
            out[pulse] = r
    return out


def estimate_offset(
    a: TimestampReport, b: TimestampReport, *, max_lag: int = 8, min_events: int = 2
) -> tuple[int | None, int]:
    """The pulse offset of camera ``b`` against camera ``a``, from shared events.

    Returns ``(lag, n)``: pulse ``p`` of ``a`` is pulse ``p + lag`` of ``b``,
    supported by ``n`` coinciding timing events; ``lag`` is None when the events
    do not decide it (too few, or two lags equally good). Events one camera has
    and the other lacks (a camera-specific late trigger) do not coincide at any
    lag, so they only add noise.
    """
    ea, eb = timing_events(a), timing_events(b)
    if not ea or not eb:
        return None, 0
    scores = []
    for lag in range(-max_lag, max_lag + 1):
        n = 0
        for p, r in ea.items():
            rb = eb.get(p + lag)
            if rb is not None and (r > 0) == (rb > 0):
                n += 1
        scores.append((n, lag))
    scores.sort(reverse=True)
    best_n, best_lag = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0
    if best_n < min_events or best_n <= runner_up:
        return None, best_n
    return best_lag, best_n
