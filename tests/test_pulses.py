"""Trigger-pulse accounting (octacam.pulses) on synthetic trigger trains.

The series reproduce what the hexaview rig's two GS3 cameras actually recorded
at 125 fps (see the missed-frames investigation): an 8 ms clock with a few µs of
jitter and ppm drift, a camera that misses a pulse (a 16 ms interval), one that
fires on the pulse's falling edge (+503 µs, then a 7.697 ms readout-limited
catch-up), and the Grasshopper3's 128 s timestamp-extension glitch.
"""

import random

import pytest

from octacam.pulses import (
    TIMESTAMP_WRAP_NS,
    PulseClock,
    PulseTracker,
    analyze_timestamps,
    estimate_offset,
)

P = 8_000_000  # 125 fps
READOUT = 7_697_000  # GS3 readout at 2048x1408: the fastest a frame can follow another


def train(n, *, period=P, ppm=0.0, jitter_ns=3_000, t0=1_800_000_000_000_000, seed=0,
          missed=(), late=None, board_late=None):
    """Camera timestamps for pulses 0..n-1 of a regular trigger train.

    ``missed``: pulses the camera never exposed. ``late``: {pulse: ns} a camera-
    side late exposure (followed by readout-limited catch-up). ``board_late``:
    {pulse: ns} a late *trigger* (every camera sees it)."""
    rng = random.Random(seed)
    scale = 1 + ppm * 1e-6
    late = dict(late or {})
    board_late = dict(board_late or {})
    out, pulses = [], []
    prev = None
    for p in range(n):
        t = t0 + p * period * scale + rng.uniform(-jitter_ns, jitter_ns)
        t += board_late.get(p, 0) + late.get(p, 0)
        if prev is not None and t - prev < READOUT:
            t = prev + READOUT  # overlapped readout delays the exposure
        if p in missed:
            continue
        out.append(int(t))
        pulses.append(p)
        prev = t
    return out, pulses


def run(ts, count=None, host=None):
    tracker = PulseTracker(PulseClock(P, count))
    got = []
    for i, t in enumerate(ts):
        a = tracker.assign(t, None if host is None else host[i])
        got.append(a.pulse)
    return tracker, got


def test_a_clean_train_maps_frame_k_to_pulse_k():
    ts, pulses = train(5000, ppm=40)
    tracker, got = run(ts, count=5000)
    assert got == pulses
    assert tracker.missed == [] and tracker.late == [] and tracker.extra == 0
    assert tracker.complete


def test_a_missed_pulse_is_detected_at_its_index():
    ts, pulses = train(3000, missed={1318})
    tracker, got = run(ts)
    assert got == pulses
    assert tracker.missed == [1318]


def test_consecutive_and_repeated_misses():
    ts, pulses = train(4000, missed={10, 11, 12, 2500, 3999})
    tracker, got = run(ts, count=4000)
    assert got == pulses
    # the last pulse is missed too: nothing delivered for it, so not complete
    assert tracker.missed == [10, 11, 12, 2500]
    assert not tracker.complete and tracker.next_pulse == 3999


def test_a_falling_edge_trigger_is_late_not_missed():
    # The top camera's signature: +503 µs, then the readout-limited catch-up.
    ts, pulses = train(2000, late={700: 503_000})
    tracker, got = run(ts)
    assert got == pulses
    assert tracker.missed == []
    assert tracker.late == [700]  # the catch-up frame (+~200 µs) is not flagged


def test_a_late_board_pulse_by_nearly_half_a_period_stays_on_its_pulse():
    # Per-interval rounding would read 11.9 ms + 4.1 ms as a miss plus an extra.
    ts, pulses = train(1000, board_late={400: 3_900_000})
    tracker, got = run(ts)
    assert got == pulses
    assert tracker.missed == [] and tracker.extra == 0


def test_drift_does_not_accumulate_across_a_long_outage():
    # 200 ppm between camera and board clocks; the camera then loses 30 s of
    # pulses. Nominal-period rounding would land ~0.75 pulse off; the tracked
    # period bridges the gap exactly.
    ts, pulses = train(20000, ppm=200, missed=set(range(8000, 11750)))
    tracker, got = run(ts)
    assert got == pulses
    assert tracker.missed == list(range(8000, 11750))


def test_a_128_second_timestamp_jump_is_folded():
    ts, pulses = train(1000)
    ts = [t + (TIMESTAMP_WRAP_NS if i >= 600 else 0) for i, t in enumerate(ts)]
    host = [i * P for i in range(len(ts))]
    tracker, got = run(ts, host=host)
    assert got == pulses
    assert tracker.missed == []
    assert [(g.pulse, g.kind) for g in tracker.glitches] == [(600, "wrap")]


def test_a_128_second_jump_is_folded_offline_too():
    ts, pulses = train(1000)
    ts = [t + (TIMESTAMP_WRAP_NS if i >= 600 else 0) for i, t in enumerate(ts)]
    report = analyze_timestamps(ts, fps=125)
    assert report.pulses == pulses and report.missed == []
    assert report.glitches[0][1] == "wrap"


def test_an_unexplained_clock_jump_is_placed_by_host_time():
    ts, pulses = train(500)
    ts = [t + (5_000_000_000 if i >= 300 else 0) for i, t in enumerate(ts)]  # +5 s
    host = [p * P for p in pulses]
    tracker, got = run(ts, host=host)
    assert got == pulses
    assert tracker.glitches[0].kind == "reanchor"


def test_a_spurious_retrigger_is_extra_not_a_new_pulse():
    ts, pulses = train(100)
    ts.insert(51, ts[50] + 2_400_000)  # a second frame 2.4 ms into pulse 50's period
    tracker, got = run(ts)
    assert got[51] is None
    assert [g for g in got if g is not None] == pulses
    assert tracker.extra == 1


def test_frames_beyond_the_train_are_extra():
    ts, _ = train(1252)
    tracker, got = run(ts, count=1250)
    assert got[-2:] == [None, None]
    assert tracker.extra == 2 and tracker.complete


def test_a_duplicate_timestamp_is_extra():
    ts, _ = train(10)
    ts.insert(5, ts[4])
    tracker, got = run(ts)
    assert got[5] is None and tracker.extra == 1


def test_assign_index_reports_leading_and_interior_misses():
    tracker = PulseTracker(PulseClock(P, 10, "software"))
    assert tracker.assign_index(2).missed == range(0, 2)
    assert tracker.assign_index(3).missed == range(0)
    assert tracker.assign_index(6).missed == range(4, 6)
    assert tracker.assign_index(6).extra
    assert tracker.assign_index(12).extra  # past the end of the train
    assert tracker.missed == [0, 1, 4, 5]


def test_expected_ts_extrapolates_the_tracked_clock():
    ts, _ = train(400, jitter_ns=0)
    tracker, _ = run(ts)
    assert tracker.expected_ts(400) == pytest.approx(ts[-1] + P, abs=1_000)


def test_estimate_offset_lines_cameras_up_on_shared_trigger_jitter():
    board = {100: 900_000, 1400: 1_500_000, 2600: 600_000}
    a, _ = train(3000, board_late=board, seed=1)
    # Camera b has one extra frame before pulse 0 (it caught a stray pulse), so
    # its frame index is one ahead of a's for every pulse.
    b, _ = train(3001, board_late={p + 1: v for p, v in board.items()}, seed=2,
                 late={2000: 503_000})
    lag, n = estimate_offset(analyze_timestamps(a, 125), analyze_timestamps(b, 125))
    assert lag == 1 and n >= 3


def test_estimate_offset_is_undecided_without_shared_events():
    a, _ = train(2000, seed=1, late={500: 503_000})
    b, _ = train(2000, seed=2)
    lag, _n = estimate_offset(analyze_timestamps(a, 125), analyze_timestamps(b, 125))
    assert lag is None


def test_a_rearmed_trigger_source_is_followed_through_its_phase_jump():
    # The pre-fix GUI start: two preview pulses, then the recording arm restarts
    # the board clock at an arbitrary phase x after the last preview pulse; the
    # cameras catch up at their readout limit (a run of 7.697 ms intervals — the
    # "dark ramp"). Every frame is still one pulse after the previous one.
    for x in (1_000_000, 3_900_000, 5_000_000, 7_500_000):
        ts, _ = train(400, board_late={p: x - P for p in range(2, 400)}, seed=x)
        tracker, got = run(ts)
        assert got == list(range(len(ts))), x
        assert tracker.missed == [] and tracker.extra == 0


def test_a_camera_outrunning_its_trigger_clock_is_flagged():
    # A pulse longer than the exposure re-triggers a GS3 at its readout limit:
    # one frame per 7.697 ms against an 8 ms clock. Each interval still rounds to
    # one pulse, so the clock check is what exposes it.
    ts = [1_000_000_000 + i * READOUT for i in range(500)]
    tracker, _ = run(ts)
    assert tracker.clock_mismatch
    clean, _ = train(500)
    assert not run(clean)[0].clock_mismatch
