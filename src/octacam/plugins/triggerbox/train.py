"""Finite trains: what a recording's arm makes `arduino/triggerbox` emit.
This module models the firmware's frame and run clocks; keep the two in step.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from octacam.plugins.triggerbox.protocol import PIN_LABELS


def period_us(fps: int) -> int:
    """The firmware's integer frame period for `fps` (triggerbox.ino's
    `(1000000 + fps / 2) / fps`): what the board actually emits.
    """
    return max(2, (1_000_000 + fps // 2) // fps)


def pulse_count(fps: int, duration_ms: int) -> int:
    """Pulses a recording of `duration_ms` at `fps` asks for:
    `round(fps * duration)`. The train the board can end on exactly may differ
    by a few above ~400 fps; `plan_train` says what it emits.
    """
    return max(1, round(duration_ms * fps / 1000))


# prepare_outputs() clamps every camera line with the firmware's default and
# minimum pulse width (kDefaultCamPulseUs / kMinCamPulseUs).
_FW_DEFAULT_PULSE_US = 500
_FW_MIN_PULSE_US = 5
# A finite run goes idle once millis() - start >= duration_ms. Its millisecond
# boundaries fall anywhere against the frame clock's t0, so the idle comes
# anywhere in the duration's last millisecond. Around that: the run clock starts
# a us or two before the frame clock (early), and the loop polls both every few
# us, emitting an edge due in the same pass before it checks the run clock
# (late; this much also covers an interrupt).
RUN_END_EARLY_US = 5
RUN_END_LATE_US = 20
# How far a recording's pulse count may move from round(fps * duration) to a
# count the board can end on exactly (only above ~400 fps; see plan_train).
MAX_COUNT_SHIFT = 10


@dataclass(frozen=True)
class TrainPlan:
    """A finite train: the `duration_ms` to arm and the pulses the board emits.

    `count` pulses on every camera line. `exact`: wherever in its last
    millisecond the run ends, every line gets exactly `count` complete pulses
    and no more. Otherwise the board's millisecond run clock cannot separate the
    last pulse from the next frame edge at this fps and `count` is the likeliest
    outcome: `certain` still guarantees it, the last pulse possibly cut short
    but to no less than half its width; without it the train may come out a
    pulse short or long. `lights_cut` lists `(light index, us)` for each
    strobe whose last on-time the run's end may cut by up to that much, and
    `light_overrun` that a strobe of the frame after the train may start before
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
        `lights` are the arm's light records.
        """
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
            f"to {cut_us} \N{MICRO SIGN}s early: it runs too close to the next frame "
            "edge for the "
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
    """`(rise, fall)` us into its frame of each camera line's pulse, clamped as
    triggerbox.ino's prepare_outputs() does. `cameras` are wire records
    `(pin_id, pulse_us, delay_us)`. A train without a camera line still counts
    frames: a zero-width pulse on every frame edge.
    """
    pulses = []
    for _pin, pulse_us, delay_us in cameras:
        delay = min(int(delay_us), period - 1)
        pulse = max(int(pulse_us) or _FW_DEFAULT_PULSE_US, _FW_MIN_PULSE_US)
        pulse = min(pulse, period - 1 - delay if period > delay + 1 else 1)
        pulses.append((delay, delay + pulse))
    return pulses or [(0, 0)]


def _strobe_windows(period: int, lights) -> list[tuple[int, int, int]]:
    """`(light index, on, off)` us into its frame of each strobing light, as the
    firmware runs it: the delay clamped into the frame, the on-time cut at the
    frame's end. Only a strobe is frame-locked: a continuous light (and a strobe
    on for a whole period, which the firmware makes one) stays on for the run,
    and a pulse train keeps its own clock for its `train_ms` or the whole run,
    so the run's end stops those wherever they are.
    """
    windows = []
    for index, (_pin, mode, delay, on_us, _p2, _p3) in enumerate(lights):
        if mode == 1 and 0 < on_us < period:
            on = min(int(delay), period - 1)
            windows.append((index, on, min(on + int(on_us), period)))
    return windows


def _end_window(duration_ms: int) -> tuple[int, int]:
    """When a run of `duration_ms` may go idle: `[first, last]` us from its
    first frame edge.
    """
    return (
        duration_ms * 1000 - 1000 - RUN_END_EARLY_US,
        duration_ms * 1000 + RUN_END_LATE_US,
    )


def _durations_ending_in(start: int, stop: int) -> tuple[int, int]:
    """The durations `(lo, hi)` (inclusive; none if lo > hi) whose run always
    ends in `[start, stop)`, in us from the train's first frame edge.
    """
    lo = -(-(start + 1000 + RUN_END_EARLY_US) // 1000)
    hi = (stop - 1 - RUN_END_LATE_US) // 1000
    return max(1, lo), hi


def plan_train(fps: int, pulses: int, cameras=(), lights=()) -> TrainPlan:
    """The run length to arm for a train of `pulses`, and what the board emits.

    `cameras` / `lights` are the arm's wire records. The board raises camera
    line `i` at `k * period + delay_i` for frame `k` and goes idle
    (everything LOW) somewhere in the duration's last millisecond, so the run
    must end after every line's last pulse has fallen and before any line's next
    one rises. That needs about a millisecond between the two. From ~400 fps
    (with the default 500 us pulse) a whole-millisecond end lands there for some
    counts only, and the nearest count within `MAX_COUNT_SHIFT` it lands for is
    taken instead. From ~650 fps (and at a few fps below it, 500 among them) it
    lands for none, and the plan is not `exact`: it takes the count and
    duration whose end is least likely to lose or add a pulse, then least likely
    to cut one, preferring the wanted count.

    The count depends on the camera lines only, so `trigger_train` (which has
    no light timing: the auto strobe duty needs the live exposures) and the arm
    agree on it. The lights only choose among the durations that end that count
    exactly: the earliest that also lets every strobe's last on-time finish
    before the next frame edge, else the latest before that edge, so the last
    strobe loses as little as possible.
    """
    period = period_us(fps)
    cams = _camera_pulses(period, cameras)
    # us into a frame by which its pulses have all risen, have all lasted half
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
        """End times (us from the first frame edge) past `into_frame` of the
        train's last frame and before the next frame's first pulse.
        """
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
    """How many us of a run's possible end times fall outside `[start, stop)`."""
    first, last = _end_window(duration_ms)
    inside = min(last, stop - 1) - max(first, start)
    return (last - first) - max(0, inside)


def _place_train_end(period: int, count: int, lo: int, hi: int, lights) -> TrainPlan:
    """Pick the duration in `[lo, hi]` (all end `count` pulses alike) that
    suits the strobes best, and report what its end does to them.
    """
    last, following = (count - 1) * period, count * period
    strobes = _strobe_windows(period, lights)
    if not strobes:
        return TrainPlan(duration_ms=lo, count=count)
    # A strobe may lose the few us by which a run can end early, immaterial to
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
