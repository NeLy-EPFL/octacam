"""Frame-rate diagnostics engine, driven entirely by the fake backend (no SDK).

Unit tests pin the pure helpers (percentiles, stage summaries, bottleneck
classification, the max-fps bisection); integration tests run the real engine
against fake cameras to prove the report shape, strict-JSON serialization, clean
teardown, and that a deliberately slow acquisition is classified as
acquisition-bound.
"""

import json
import math
import os
import time

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam import diagnostics as dg
from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings, StartResult

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]


@pytest.fixture
def fake_system(tmp_path):
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    assert len(system) == 2
    system.load_config(tmp_path)
    for camera in system:  # tiny frames so x264 encodes trivially fast in CI
        camera.set_geometry(width=160, height=120)
    yield system
    system.close()


# --------------------------------------------------------------------- helpers


def _trial(
    achieved: float, target: float, drop: float = 0.0, qmax: int = 0
) -> dg.CameraTrial:
    return dg.CameraTrial(
        serial="S",
        name="cam",
        width=160,
        height=120,
        target_fps=target,
        achieved_fps=achieved,
        grabbed=int(achieved),
        dropped=int(achieved * drop),
        drop_rate=drop,
        max_queue_depth=qmax,
        stages={},
    )


def _outcome(
    achieved: float, target: float, drop: float = 0.0, qmax: int = 0
) -> dg.TrialOutcome:
    return dg.TrialOutcome(
        trials=[_trial(achieved, target, drop, qmax)],
        jitter_p99_ms=None,
        cpu_percent=None,
    )


# ----------------------------------------------------------------- pure helpers


def test_pct_interpolates_and_edges():
    assert dg._pct([], 50) == 0.0
    assert dg._pct([5.0], 99) == 5.0
    vals = [float(i) for i in range(1, 101)]  # 1..100
    assert dg._pct(vals, 50) == pytest.approx(50.5)
    assert dg._pct(vals, 99) == pytest.approx(99.01, abs=0.05)
    assert dg._pct(vals, 100) == 100.0


def test_stage_timing_summary():
    empty = dg._stage_timing("acquire", [])
    assert empty.samples == 0 and empty.mean_ms == 0.0
    assert not math.isfinite(empty.implied_max_fps)  # 1000/0 -> inf

    ns = [1_000_000, 2_000_000, 3_000_000]  # 1, 2, 3 ms
    s = dg._stage_timing("acquire", ns)
    assert s.samples == 3
    assert s.mean_ms == pytest.approx(2.0)
    assert s.p50_ms == pytest.approx(2.0)
    assert s.max_ms == pytest.approx(3.0)
    assert s.implied_max_fps == pytest.approx(500.0)  # 1000 / 2 ms


def test_finite_normalizes_non_finite():
    assert dg._finite(float("inf")) is None
    assert dg._finite(float("nan")) is None
    assert dg._finite(123.456, 1) == 123.5


def test_stage_timing_to_dict_has_no_infinity():
    d = dg._stage_timing("transform", []).to_dict()
    assert d["implied_max_fps"] is None  # inf serialized as null
    json.dumps(d, allow_nan=False)  # would raise if any inf/nan leaked


# -------------------------------------------------------------- classification


def test_classify_acquisition_bound():
    ceilings = dg.Ceilings(grab_fps={"S": 50.0}, encode_fps={"S": 500.0})
    achievable, bottleneck, recs = dg._classify(200.0, ceilings, _outcome(50.0, 200.0))
    assert not achievable
    assert bottleneck == dg.ACQUISITION
    assert any("cquisition" in r for r in recs)


def test_classify_encode_bound():
    ceilings = dg.Ceilings(grab_fps={"S": 500.0}, encode_fps={"S": 50.0})
    achievable, bottleneck, recs = dg._classify(200.0, ceilings, _outcome(50.0, 200.0))
    assert not achievable
    assert bottleneck == dg.ENCODE
    assert any("ncode" in r for r in recs)


def test_classify_host_bound():
    # Both stages clear the target in isolation, but the full system falls short.
    ceilings = dg.Ceilings(grab_fps={"S": 500.0}, encode_fps={"S": 500.0})
    achievable, bottleneck, _ = dg._classify(
        200.0, ceilings, _outcome(150.0, 200.0, drop=0.05)
    )
    assert not achievable
    assert bottleneck == dg.HOST


def test_classify_achievable_is_none():
    ceilings = dg.Ceilings(grab_fps={"S": 500.0}, encode_fps={"S": 500.0})
    achievable, bottleneck, recs = dg._classify(100.0, ceilings, _outcome(100.0, 100.0))
    assert achievable
    assert bottleneck == dg.NONE
    assert recs == []


def test_classify_transfer_bound_when_cameras_throttle_each_other():
    # Acquisition-limited AND the concurrent rate is well below the solo rate
    # (cameras share more bus bandwidth than the link provides) → TRANSFER, not
    # a per-camera ACQUISITION limit.
    ceilings = dg.Ceilings(
        grab_fps={"A": 50.0, "B": 50.0},
        encode_fps={"A": 500.0, "B": 500.0},
        grab_solo_fps={"A": 100.0, "B": 100.0},  # 50 << 100*0.85 → contended
    )
    achievable, bottleneck, recs = dg._classify(
        200.0, ceilings, _outcome(50.0, 200.0), throughput_total=400.0
    )
    assert not achievable
    assert bottleneck == dg.TRANSFER
    assert any("Transfer-bound" in r and "400 MB/s" in r for r in recs)


def test_classify_host_vs_transfer_split():
    # Both stages clear the target alone, but the system falls short. With no
    # solo evidence of contention it's HOST; with concurrent << solo it's TRANSFER.
    base = dict(grab_fps={"S": 500.0}, encode_fps={"S": 500.0})
    host = dg.Ceilings(**base)
    _, b_host, _ = dg._classify(200.0, host, _outcome(150.0, 200.0, drop=0.05))
    assert b_host == dg.HOST

    transfer = dg.Ceilings(**base, grab_solo_fps={"S": 700.0})  # 500 < 700*0.85
    _, b_transfer, _ = dg._classify(200.0, transfer, _outcome(150.0, 200.0, drop=0.05))
    assert b_transfer == dg.TRANSFER


def test_ceilings_bus_contended_property():
    contended = dg.Ceilings(
        grab_fps={"S": 50.0}, encode_fps={}, grab_solo_fps={"S": 100.0}
    )
    assert contended.bus_contended
    clear = dg.Ceilings(
        grab_fps={"S": 95.0}, encode_fps={}, grab_solo_fps={"S": 100.0}
    )
    assert not clear.bus_contended  # 95 > 100 * CONTENTION_RATIO
    no_solo = dg.Ceilings(grab_fps={"S": 50.0}, encode_fps={})
    assert not no_solo.bus_contended  # inert without a solo measurement


def test_throughput_mbps():
    per_cam, total = dg._throughput_mbps(
        {"A": 100.0, "B": 50.0}, {"A": (1000, 1000), "B": (1000, 1000)}
    )
    # 1e6 px (Mono8 = 1 B/px) × 100 fps / 1e6 = 100 MB/s.
    assert per_cam["A"] == pytest.approx(100.0)
    assert per_cam["B"] == pytest.approx(50.0)
    assert total == pytest.approx(150.0)


def test_find_max_fps_bisects_to_threshold():
    # A probe that passes below 137 fps; the bisection should land just under it.
    calls = []

    def probe(fps: float) -> bool:
        calls.append(fps)
        return fps <= 137.0

    result = dg.find_max_fps(probe, lo=100.0, hi=200.0, iterations=6)
    assert 130.0 <= result <= 137.0
    assert result <= 137.0  # never reports a failing rate as achievable


def test_find_max_fps_hi_passes_returns_hi():
    result = dg.find_max_fps(lambda fps: True, lo=100.0, hi=200.0)
    assert result == 200.0


def test_reconcile_stable_max_reports_achieved_not_target():
    # A trial passes at 97% of its target, so the winning *target* (64) can sit
    # above what was actually acquired (63). The reported max must be the achieved
    # rate, never the target — otherwise it reads above the acquisition ceiling.
    confirm = _outcome(achieved=63.0, target=64.0)
    assert confirm.stable_passed
    fps, confirmed = dg._reconcile_stable_max(64.0, confirm, lo=30.0, ceiling_cap=90.0)
    assert confirmed
    assert fps == pytest.approx(63.0)


def test_reconcile_stable_max_capped_by_ceiling():
    # Even a high sustained rate cannot exceed the isolated acquisition/encode cap.
    confirm = _outcome(achieved=99.0, target=100.0)
    fps, confirmed = dg._reconcile_stable_max(100.0, confirm, lo=30.0, ceiling_cap=63.0)
    assert confirmed
    assert fps == pytest.approx(63.0)


def test_reconcile_stable_max_unconfirmed_backs_off():
    # A candidate that fails the longer confirmation is reported below the target.
    confirm = _outcome(achieved=63.0, target=64.0, drop=0.005)  # >0.1% -> not stable
    assert confirm.passed and not confirm.stable_passed
    fps, confirmed = dg._reconcile_stable_max(64.0, confirm, lo=30.0, ceiling_cap=90.0)
    assert not confirmed
    assert fps < 64.0


# ------------------------------------------------------------- stable pass bar


def test_stable_passed_requires_headroom():
    # A queue climbing toward its bound is unstable even with no drops yet.
    guard_trip = int(dg.WRITER_QUEUE_SIZE * dg.QUEUE_SATURATION_FRACTION)
    hot = _outcome(100.0, 100.0, drop=0.0, qmax=guard_trip)
    assert hot.passed  # meets the loose achievable bar
    assert not hot.stable_passed  # ...but the queue is saturating

    cool = _outcome(100.0, 100.0, drop=0.0, qmax=1)
    assert cool.passed and cool.stable_passed


def test_stable_passed_tighter_drop_bar():
    # 0.5% drops: achievable (<1%) but not stable (>0.1%).
    marginal = _outcome(100.0, 100.0, drop=0.005, qmax=0)
    assert marginal.passed
    assert not marginal.stable_passed


def test_outcome_max_queue_depth_is_worst_camera():
    outcome = dg.TrialOutcome(
        trials=[_trial(100, 100, qmax=3), _trial(100, 100, qmax=17)],
        jitter_p99_ms=None,
        cpu_percent=None,
    )
    assert outcome.max_queue_depth == 17


# ----------------------------------------------------------------- progress


def test_progress_plan_advances_and_animates():
    plan = dg._ProgressPlan([("a", 1.0), ("b", 3.0)])
    p1 = plan.step("a", "x")
    assert p1.fraction == 0.0 and p1.target == pytest.approx(0.25) and p1.eta_s == 1.0
    # Re-emitting the same phase keeps the fractions but updates the detail.
    p1b = plan.step("a", "y")
    assert p1b.detail == "y" and p1b.fraction == 0.0
    p2 = plan.step("b", "")
    assert p2.fraction == pytest.approx(0.25) and p2.target == pytest.approx(1.0)
    done = plan.done()
    assert done.fraction == 1.0 and done.target == 1.0


def test_progress_plan_advance_creeps_forward_without_reset():
    # A multi-probe phase (the max-fps search) must ease forward across emits, not
    # re-emit its start fraction — that reset is what jerked the bar backwards.
    plan = dg._ProgressPlan([("trial", 1.0), ("max", 3.0)])  # total 4
    plan.step("trial", "")
    emits = [plan.step("max", f"probe {i}", advance=True, eta=0.5) for i in range(4)]
    fracs = [e.fraction for e in emits]
    targets = [e.target for e in emits]
    assert fracs == sorted(fracs)  # never regresses
    assert targets == sorted(targets)
    assert fracs[0] == pytest.approx(0.25)  # the max phase starts at 1/4
    # Each probe picks up exactly where the previous one was heading.
    for i in range(1, len(emits)):
        assert fracs[i] == pytest.approx(targets[i - 1])
    assert all(e.eta_s == 0.5 for e in emits)
    assert all(0.25 <= f < 1.0 for f in fracs)
    assert targets[-1] < 1.0  # leaves headroom for the Done sentinel to finish


def test_progress_plan_skips_absent_phase():
    # A label that jumps ahead (a conditional phase that did not run) still lands.
    plan = dg._ProgressPlan([("a", 1.0), ("b", 1.0), ("c", 2.0)])
    plan.step("a", "")
    p = plan.step("c", "")  # 'b' skipped
    assert p.fraction == pytest.approx(0.5)  # (1 + 1) / 4


def test_progress_to_dict_is_strict_json():
    d = dg.Progress("phase", "detail", 0.25, 0.5, 2.0).to_dict()
    assert d == {
        "phase": "phase",
        "detail": "detail",
        "fraction": 0.25,
        "target": 0.5,
        "eta_s": 2.0,
    }
    json.dumps(d, allow_nan=False)


def test_null_writer_counts_frames():
    import numpy as np

    w = dg._NullWriter(profile=True)
    assert w.open("ignored", 100.0, (16, 16))
    frame = np.zeros((16, 16), dtype=np.uint8)
    for _ in range(5):
        assert w.write(frame)
    w.close()
    assert w.frames_written == 5


# ---------------------------------------------------------------- integration


def test_diagnose_report_structure_null_sink(fake_system):
    settings = RecordingSettings(fps=120.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=120.0,
        duration_s=0.3,
        find_max=False,
        sink="null",
    )
    assert report.n_cameras == 2
    assert report.backend == "fake"
    assert len(report.trials) == 2
    assert report.achievable  # the fake keeps up trivially
    assert report.bottleneck == dg.NONE
    assert report.achieved_fps == pytest.approx(120.0, rel=0.15)
    assert report.ceilings is not None
    # null sink -> the encoder is not exercised, only acquisition is measured.
    assert report.ceilings.encode_fps == {}
    for trial in report.trials:
        assert set(trial.stages) == {"acquire", "transform", "enqueue", "encode"}

    # The whole report must serialize as strict JSON (no Infinity/NaN tokens).
    payload = json.dumps(report.to_dict(), allow_nan=False)
    assert json.loads(payload)["bottleneck"] == "none"


def test_diagnose_leaves_cameras_stopped(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        sink="null",
    )
    for camera in fake_system:
        assert not camera.backend.is_grabbing()
    # Preview must still start cleanly afterwards (backends left in a good state).
    fake_system.start_preview()
    for camera in fake_system:
        assert camera.backend.is_grabbing()
    fake_system.stop()


def test_diagnose_acquisition_bound_with_slow_retrieve(fake_system):
    # Make each camera slow to deliver a frame (~50 fps ceiling) so a 200 fps
    # target is unmistakably acquisition-bound.
    for camera in fake_system:
        backend = camera.backend
        original = backend.retrieve

        def slow(timeout_ms, wants_array, _orig=original):
            time.sleep(0.02)
            return _orig(timeout_ms, wants_array)

        backend.retrieve = slow

    settings = RecordingSettings(fps=200.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=200.0,
        duration_s=0.4,
        find_max=False,
        sink="null",
    )
    assert not report.achievable
    assert report.bottleneck == dg.ACQUISITION
    assert report.predicted_max_fps is not None
    assert report.predicted_max_fps < 200.0
    assert report.achieved_fps < 200.0
    assert any("cquisition" in r for r in report.recommendations)


def test_diagnose_external_trigger_skips_max_search(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="external")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=True,  # requested, but the max search must be skipped for external
        sink="null",
    )
    # Ceilings are still measured (a software-triggered lower bound), but the
    # max-fps sweep is meaningless for a hardware-clocked rig and is skipped.
    assert report.ceilings is not None
    assert report.measured_max_fps is None
    assert any("external hardware trigger" in n for n in report.notes)
    # ...but the free-run ceiling still gives the external rig a hardware max.
    assert report.ceilings.freerun_fps
    assert report.hardware_max_fps is not None


# --------------------------------------------------------------- free-run ceiling


def test_diagnose_measures_freerun_and_hardware_max(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        measure_freerun=True,
        sink="null",
    )
    assert set(report.ceilings.freerun_fps) == set(FAKE_SERIALS)
    assert all(v > 0 for v in report.ceilings.freerun_fps.values())
    assert report.freerun_max_fps is not None and report.freerun_max_fps > 0
    # null sink -> the encoder is not measured, so the hardware max is the
    # free-run acquisition ceiling alone.
    assert report.hardware_max_fps == pytest.approx(report.freerun_max_fps)
    # The whole report (with the new free-run/hardware fields) is strict JSON.
    payload = json.loads(json.dumps(report.to_dict(), allow_nan=False))
    assert payload["ceilings"]["freerun_min"] is not None


def test_diagnose_freerun_disabled(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        duration_s=0.3,
        find_max=False,
        measure_freerun=False,
        sink="null",
    )
    assert report.ceilings.freerun_fps == {}
    assert report.freerun_max_fps is None
    assert report.hardware_max_fps is None


def test_diagnose_freerun_unsupported_backend_noted(fake_system):
    # A backend without the free-run seam is skipped with a note, never a crash.
    for camera in fake_system:
        camera.backend.begin_freerun = None  # shadow the method → "unsupported"
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        duration_s=0.3,
        find_max=False,
        measure_freerun=True,
        sink="null",
    )
    assert report.ceilings.freerun_fps == {}
    assert report.freerun_max_fps is None
    assert any("does not support free-run" in n for n in report.notes)


def test_measure_grab_ceiling_solo(fake_system):
    solo = dg.measure_grab_ceiling_solo(list(fake_system), 0.2, warmup_s=0.1)
    assert set(solo) == set(FAKE_SERIALS)
    assert all(v > 0 for v in solo.values())


def test_diagnose_measures_solo_ceiling_and_throughput(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        sink="null",
    )
    # ≥2 cameras → the solo pass runs, and throughput is derived from the ceiling.
    assert set(report.ceilings.grab_solo_fps) == set(FAKE_SERIALS)
    assert set(report.throughput_mbps) == set(FAKE_SERIALS)
    assert report.throughput_mbps_total > 0
    payload = json.loads(json.dumps(report.to_dict(), allow_nan=False))
    assert payload["throughput_mbps_total"] is not None
    assert payload["ceilings"]["grab_solo_min"] is not None


def test_diagnose_freerun_trial_measures_hardware_max(fake_system):
    # A real sink runs the free-run end-to-end trial (encoder in the loop), which
    # upgrades the hardware max to a measured, drop-adjusted rate.
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        measure_freerun=True,
        sink="config",
    )
    assert len(report.freerun_trials) == len(FAKE_SERIALS)
    assert report.hardware_max_fps is not None and report.hardware_max_fps > 0
    payload = json.loads(json.dumps(report.to_dict(), allow_nan=False))
    assert len(payload["freerun_trials"]) == len(FAKE_SERIALS)
    # The trial leaves every backend stopped, like the software scenarios.
    for camera in fake_system:
        assert not camera.backend.is_grabbing()


def test_run_target_trial_aborts_when_writer_open_fails(fake_system, monkeypatch):
    # A failed encoder open (ffmpeg missing / bad params) must abort the trial
    # with a clear setup error, not proceed and misreport ~100% ENCODE drops.
    from types import SimpleNamespace

    closed: list = []

    class BadWriter:
        frames_written = 0

        def open(self, *a):
            return False  # _open_sink failed

        def close(self):
            closed.append(self)

    monkeypatch.setattr(dg, "_make_writer", lambda vf, *, profile: BadWriter())

    with pytest.raises(RuntimeError, match="writer failed to open"):
        dg.run_target_trial(
            list(fake_system), SimpleNamespace(extension="mkv"), 60.0, 0.2
        )
    # Every writer created for the trial is closed before the abort propagates,
    # and no backend was left grabbing.
    assert len(closed) == len(FAKE_SERIALS)
    for camera in fake_system:
        assert not camera.backend.is_grabbing()


def test_run_target_trial_tears_down_on_arm_failure(fake_system, monkeypatch):
    # A per-camera arm failure after the writers are open must tear everything
    # down (stop every backend, close every writer) before the error propagates.
    cams = list(fake_system)
    monkeypatch.setattr(
        cams[0].backend,
        "start_grab_record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bus dropped")),
    )
    with pytest.raises(RuntimeError, match="bus dropped"):
        dg.run_target_trial(cams, None, 60.0, 0.2)  # null sink
    for camera in fake_system:
        assert not camera.backend.is_grabbing()


def test_measure_grab_ceiling_arm_failure_drops_camera(fake_system, monkeypatch):
    # A camera whose arm raises is dropped from the results (not counted), but
    # its stop_grab still runs in the finally so no backend is left grabbing.
    cams = list(fake_system)
    monkeypatch.setattr(
        cams[0].backend,
        "start_grab_preview",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("arm failed")),
    )
    fps = dg.measure_grab_ceiling(cams, 0.2, warmup_s=0.1)
    assert cams[0].serial_number not in fps  # arm-failed camera dropped
    assert cams[1].serial_number in fps  # the healthy one still measured
    for camera in fake_system:
        assert not camera.backend.is_grabbing()


def test_diagnose_notes_camera_missing_from_grab_ceiling(fake_system, monkeypatch):
    # When a camera is missing from the grab ceiling (it failed to arm), the
    # runner must flag it — otherwise grab_min silently improves over the
    # survivors. Simulate the drop at the measure boundary so the full pipeline
    # (which shares the fake backend's arm path) still runs and diagnose completes.
    real = dg.measure_grab_ceiling
    dropped = FAKE_SERIALS[0]

    def drop_one(cameras, *args, **kwargs):
        result = real(cameras, *args, **kwargs)
        result.pop(dropped, None)
        return result

    monkeypatch.setattr(dg, "measure_grab_ceiling", drop_one)

    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        sink="null",
    )
    assert dropped not in report.ceilings.grab_fps
    assert any("failed to arm" in n for n in report.notes)


def test_diagnose_warns_on_high_machine_load(fake_system, monkeypatch):
    # A machine already busy before the run skews results → a warning is emitted.
    monkeypatch.setattr(dg, "_probe_system_load", lambda: (95.0, 3.0))
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    report = dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        sink="null",
    )
    assert report.system_cpu_percent == 95.0
    assert report.load_per_core == 3.0
    assert any("CPU-busy" in r for r in report.recommendations)


def test_diagnose_emits_monotonic_progress(fake_system):
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    updates: list[dg.Progress] = []
    dg.diagnose(
        fake_system,
        settings,
        target_fps=60.0,
        duration_s=0.3,
        find_max=False,
        measure_freerun=True,
        sink="null",
        progress_cb=updates.append,
    )
    assert updates
    fracs = [u.fraction for u in updates]
    assert fracs == sorted(fracs)  # never regresses
    assert updates[-1].fraction == 1.0  # the Done sentinel
    labels = {u.phase for u in updates}
    assert dg.PHASE_ACQUIRE in labels
    assert dg.PHASE_FREERUN in labels


# ------------------------------------------------------- controller integration


def test_run_diagnostic_via_controller(fake_system):
    settings = RecordingSettings(fps=100.0, trigger_source="software")
    controller = RecordingController(fake_system, settings, auto_preview=True)
    controller.start_preview()
    assert controller.state == "preview"

    got: list[dict] = []
    controller.add_listener(
        lambda kind, payload: got.append(payload) if kind == "diagnostics" else None
    )

    result = controller.run_diagnostic(duration_s=0.3, find_max=False, sink="null")
    assert result.ok
    assert controller.diagnosing and controller.state == "diagnosing"

    # A second benchmark and camera control are both refused while one runs.
    assert controller.run_diagnostic().status == StartResult.BUSY
    assert controller.start_recording().status == StartResult.BUSY
    with pytest.raises(RuntimeError):
        controller.set_camera_param(0, "exposure", 2000.0)

    deadline = time.time() + 30
    while controller.diagnosing and time.time() < deadline:
        time.sleep(0.05)
    assert controller.state == "preview"  # preview resumed cleanly
    assert controller.get_last_diagnostic() is not None
    assert got and got[0]["backend"] == "fake"
    # camera control works again once the benchmark is done
    controller.set_camera_param(0, "exposure", 2000.0)


def test_benchmark_preview_rearm_failure_leaves_idle(fake_system, monkeypatch):
    """A camera dropping out during a benchmark can make the finally-block preview
    re-arm raise; the diagnostic thread must still leave the "diagnosing" state
    (dropping the camera lock) rather than wedging the controller."""
    settings = RecordingSettings(fps=60.0, trigger_source="software")
    controller = RecordingController(fake_system, settings, auto_preview=True)

    def boom(*a, **k):
        raise RuntimeError("camera dropped during re-arm")

    monkeypatch.setattr(fake_system, "start_preview", boom)

    assert controller.run_diagnostic(duration_s=0.3, find_max=False, sink="null").ok
    deadline = time.time() + 30
    while controller.diagnosing and time.time() < deadline:
        time.sleep(0.05)

    assert controller.state == "idle"  # not wedged in "diagnosing"
    assert not controller._camera_locked
    # Camera control (and a recording) work again once the benchmark is done.
    controller.set_camera_param(0, "exposure", 2000.0)


def test_run_diagnostic_emits_progress_notifications(fake_system):
    settings = RecordingSettings(fps=80.0, trigger_source="software")
    controller = RecordingController(fake_system, settings, auto_preview=True)
    controller.start_preview()

    progress: list[dict] = []
    controller.add_listener(
        lambda kind, payload: (
            progress.append(payload) if kind == "diagnostics_progress" else None
        )
    )

    controller.run_diagnostic(duration_s=0.3, find_max=False, sink="null")
    deadline = time.time() + 30
    while controller.diagnosing and time.time() < deadline:
        time.sleep(0.05)

    assert progress  # the Benchmark tab's determinate bar is fed these
    assert all({"phase", "fraction", "target", "eta_s"} <= set(p) for p in progress)
    assert progress[-1]["fraction"] == 1.0  # ends on the Done sentinel


def test_run_diagnostic_rejected_while_recording(fake_system, tmp_path):
    settings = RecordingSettings(
        fps=60.0, duration_s=5.0, save_dir=str(tmp_path / "rec")
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording(confirm_overwrite=True).ok
    try:
        assert controller.run_diagnostic().status == StartResult.BUSY
    finally:
        controller.stop_recording(abort=True)
        controller.join(timeout=20)


def test_diagnostics_rest_endpoints(fake_system, tmp_path):
    from fastapi.testclient import TestClient

    from octacam.config import OctacamConfig
    from octacam.web.app import create_app

    settings = RecordingSettings(fps=80.0, save_dir=str(tmp_path / "rec"))
    controller = RecordingController(fake_system, settings)
    controller.start_preview()
    app = create_app(controller, OctacamConfig(), None, config_dir=str(tmp_path))
    try:
        with TestClient(app) as client:
            # Request validation (extra="forbid" + positive-value validators).
            assert (
                client.post("/api/diagnostics/run", json={"duration_s": 0}).status_code
                == 422
            )
            assert (
                client.post("/api/diagnostics/run", json={"bogus": 1}).status_code
                == 422
            )

            r = client.post(
                "/api/diagnostics/run",
                json={"duration_s": 0.3, "find_max": False, "sink": "null"},
            )
            assert r.status_code == 202
            # A second run and a shutdown are both refused while one is active.
            assert client.post("/api/diagnostics/run", json={}).status_code == 409
            assert client.post("/api/shutdown").status_code == 409

            deadline = time.time() + 30
            while controller.diagnosing and time.time() < deadline:
                time.sleep(0.05)
            last = client.get("/api/diagnostics/last").json()
            assert last.get("backend") == "fake"
            assert "trials" in last
    finally:
        controller.close()
