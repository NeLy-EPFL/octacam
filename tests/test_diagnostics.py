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


def _trial(achieved: float, target: float, drop: float = 0.0) -> dg.CameraTrial:
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
        max_queue_depth=0,
        stages={},
    )


def _outcome(achieved: float, target: float, drop: float = 0.0) -> dg.TrialOutcome:
    return dg.TrialOutcome(
        trials=[_trial(achieved, target, drop)], jitter_p99_ms=None, cpu_percent=None
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
