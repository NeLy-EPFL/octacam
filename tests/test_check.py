"""`octacam check`: screening recordings for missed pulses and desync."""

import json
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from octacam.check import check_recording, find_recordings
from octacam.cli import app

P = 8_000_000  # 125 fps


def _write(folder, cameras, *, schema=3, extra_summary=None, arrays_extra=None):
    """A minimal recording folder: summary + timestamps.npz for ``cameras``
    ({name: timestamps})."""
    folder.mkdir(parents=True)
    summary = {
        "schema_version": schema,
        "fps_target": 125.0,
        "cameras": [{"name": n, "frames": len(t)} for n, t in cameras.items()],
        **(extra_summary or {}),
    }
    (folder / "recording_summary.json").write_text(json.dumps(summary))
    arrays = {
        f"{n}/timestamp_ns": np.asarray(t, dtype=np.int64) for n, t in cameras.items()
    }
    arrays.update(arrays_extra or {})
    np.savez_compressed(folder / "timestamps.npz", **arrays)
    return folder


def _train(n, missed=(), t0=10**15, events=None, first=0):
    events = events or {}
    return [t0 + p * P + events.get(p, 0) for p in range(first, n) if p not in missed]


def test_a_clean_pair_is_ok(tmp_path):
    rec = _write(
        tmp_path / "a", {"top": _train(2000), "bottom": _train(2000, t0=5 * 10**15)}
    )
    result = check_recording(rec)
    assert result.ok, result.problems
    assert [c.missed for c in result.cameras] == [[], []]


def test_missed_pulses_and_unequal_counts_are_problems(tmp_path):
    rec = _write(
        tmp_path / "a",
        {"top": _train(2000, missed={665, 1283}), "bottom": _train(2000)},
    )
    result = check_recording(rec)
    top = result.cameras[0]
    assert top.missed == [665, 1283]
    assert top.missed_after_frame == [664, 1281]
    assert not result.ok
    assert any("unequal frame counts" in p for p in result.problems)
    assert any("top missed 2 pulse(s)" in p for p in result.problems)


def test_a_start_offset_is_found_from_shared_timing_events(tmp_path):
    # The board's late pulses reach both cameras at the same pulse; bottom caught
    # one extra pulse before the train, so its frame k is top's frame k-1... i.e.
    # every shared event sits one index later in bottom.
    events = {100: 900_000, 900: 1_500_000, 1500: 600_000}
    top = _train(2000, events=events)
    bottom = [t - P for t in _train(2001, events={p + 1: v for p, v in events.items()})]
    rec = _write(tmp_path / "a", {"top": top, "bottom": bottom[:2000]})
    result = check_recording(rec)
    # The recorder's convention: pulses after the earliest camera (bottom).
    assert [c.start_offset for c in result.cameras] == [1, 0]
    assert any("top is offset from bottom from the start" in p for p in result.problems)


def test_a_128_second_clock_jump_is_a_warning_not_a_miss(tmp_path):
    top = _train(2000)
    top = [t + (128_000_000_000 if i >= 800 else 0) for i, t in enumerate(top)]
    rec = _write(tmp_path / "a", {"top": top, "bottom": _train(2000)})
    result = check_recording(rec)
    assert result.ok, result.problems
    assert any("clock jump" in w for w in result.warnings)


def test_recorded_accounting_is_reported_as_filled(tmp_path):
    # A schema-4 recording filled its missed pulse: aligned, only a warning.
    ts = np.asarray(_train(1000), dtype=np.int64)
    missed = np.zeros(1000, dtype=bool)
    missed[400] = True
    rec = _write(
        tmp_path / "a",
        {"top": ts, "bottom": ts + 10**12},
        schema=4,
        extra_summary={"pulse_train": {"fill": True, "count": 1000}},
        arrays_extra={
            "top/missed": missed,
            "top/dropped": missed,
            "top/pulse_index": np.arange(1000),
            "bottom/missed": np.zeros(1000, dtype=bool),
            "bottom/dropped": np.zeros(1000, dtype=bool),
            "bottom/pulse_index": np.arange(1000),
        },
    )
    result = check_recording(rec)
    assert result.ok, result.problems
    top = result.cameras[0]
    assert top.source == "recorded" and top.filled and top.missed == [400]
    assert any("filled with the previous frame" in w for w in result.warnings)


def test_find_recordings_walks_directories(tmp_path):
    for name in ("Fly2/001", "Fly10/001", "Fly1/001"):
        _write(tmp_path / name, {"top": _train(10)})
    found = [p.relative_to(tmp_path).as_posix() for p in find_recordings([tmp_path])]
    assert found == ["Fly1/001", "Fly2/001", "Fly10/001"]


def test_cli_exits_nonzero_on_a_problem(tmp_path):
    _write(tmp_path / "good", {"top": _train(500), "bottom": _train(500)})
    runner = CliRunner()
    ok = runner.invoke(app, ["check", str(tmp_path / "good")])
    assert ok.exit_code == 0, ok.output
    _write(tmp_path / "bad", {"top": _train(500, missed={100}), "bottom": _train(500)})
    bad = runner.invoke(app, ["check", str(tmp_path)])
    assert bad.exit_code == 1
    assert "PROBLEM" in bad.output
    as_json = runner.invoke(app, ["check", "--json", str(tmp_path / "bad")])
    data = json.loads(as_json.output)
    assert data[0]["cameras"][0]["missed_pulses"] == 1
    assert data[0]["cameras"][0]["missed_pulse_indices"] == [100]


# --------------------------------------------------------------- schema 4


def _cam4(name, frames, **fields):
    """A schema-4 per-camera summary entry (clean unless ``fields`` say otherwise)."""
    return {
        "name": name,
        "frames": frames,
        "missed_pulses": 0,
        "missed_pulse_indices": [],
        "writer_dropped": 0,
        "late_frames": 0,
        "late_pulse_indices": [],
        "clock_mismatch": False,
        "timestamp_glitches": [],
        "unclocked_frames": 0,
        "start_offset_pulses": 0,
        "timestamp_source": "hardware",
        **fields,
    }


def _summary4(folder, cameras, *, fill=True, count=1000, sync=None, **extra):
    """A schema-4 recording folder holding only its summary (save_timestamps off,
    the default)."""
    folder.mkdir(parents=True)
    summary = {
        "schema_version": 4,
        "aborted": False,
        "fps_target": 125.0,
        "duration_s": 8.0,
        "trigger_source": "managed" if fill else "external",
        "pulse_train": {"fill": fill, "count": count if fill else None},
        "sync": sync or {"ok": True, "warnings": []},
        "cameras": cameras,
        **extra,
    }
    (folder / "recording_summary.json").write_text(json.dumps(summary))
    return folder


def _patch_summary(folder, camera, **fields):
    path = folder / "recording_summary.json"
    summary = json.loads(path.read_text())
    summary["cameras"][camera].update(fields)
    path.write_text(json.dumps(summary))


def test_a_schema4_summary_is_authoritative_without_timestamps(tmp_path):
    rec = _summary4(
        tmp_path / "a",
        [
            _cam4("top", 1000),
            _cam4(
                "bottom",
                1000,
                missed_pulses=37,
                missed_pulse_indices=list(range(100, 137)),
                start_offset_pulses=1,
                clock_mismatch=True,
            ),
        ],
        sync={"ok": False, "warnings": ["Camera bottom started 1 pulse(s) late"]},
    )
    result = check_recording(rec)
    assert not result.ok
    bottom = result.cameras[1]
    assert bottom.source == "summary" and bottom.missed_count == 37
    assert bottom.start_offset == 1
    assert any("bottom started 1 pulse(s) late" in p for p in result.problems)
    assert any("bottom: frames did not follow" in p for p in result.problems)
    assert any(
        p.startswith("the recorder's sync check failed: Camera bottom started")
        for p in result.problems
    )
    assert any("bottom missed 37 pulse(s)" in w for w in result.warnings)
    assert not any("timestamps.npz" in w for w in result.warnings)


def test_a_failed_sync_verdict_alone_is_a_problem(tmp_path):
    reason = "Camera bottom: could not verify that its first frame is the others'"
    rec = _summary4(
        tmp_path / "a",
        [_cam4("top", 1000), _cam4("bottom", 1000, start_offset_pulses=None)],
        sync={"ok": False, "warnings": [reason]},
    )
    result = check_recording(rec)
    assert result.problems == [f"the recorder's sync check failed: {reason}"]


def test_a_clean_schema4_summary_is_ok(tmp_path):
    rec = _summary4(tmp_path / "a", [_cam4("top", 1000), _cam4("bottom", 1000)])
    result = check_recording(rec)
    assert result.ok, result.problems
    assert result.warnings == []


def test_sync_verdict_and_unclocked_frames_are_honored_with_timestamps(tmp_path):
    ts = np.asarray(_train(1000), dtype=np.int64)
    accounting = {
        f"{n}/{k}": v
        for n in ("top", "bottom")
        for k, v in (
            ("missed", np.zeros(1000, dtype=bool)),
            ("dropped", np.zeros(1000, dtype=bool)),
            ("pulse_index", np.arange(1000)),
        )
    }
    rec = _write(
        tmp_path / "a",
        {"top": ts, "bottom": ts + 10**12},
        schema=4,
        extra_summary={
            "pulse_train": {"fill": True, "count": 1000},
            "sync": {"ok": False, "warnings": ["Camera top: 5 frame(s) had no ..."]},
        },
        arrays_extra=accounting,
    )
    _patch_summary(rec, 0, unclocked_frames=5)
    result = check_recording(rec)
    assert [c.source for c in result.cameras] == ["recorded", "recorded"]
    assert any(
        "top: 5 frame(s) had no hardware timestamp" in p for p in result.problems
    )
    assert any("the recorder's sync check failed" in p for p in result.problems)


def test_counts_come_from_the_summary_not_its_capped_index_lists(tmp_path):
    # An external (unfilled) take: the summary's lists stop at 1000 entries, its
    # counts do not.
    rec = _summary4(
        tmp_path / "a",
        [
            _cam4("top", 20000),
            _cam4(
                "bottom",
                18500,
                missed_pulses=1500,
                missed_pulse_indices=list(range(0, 2000, 2)),
                late_frames=1200,
                late_pulse_indices=list(range(1, 2001, 2)),
            ),
        ],
        fill=False,
        duration_s=160.0,
    )
    result = check_recording(rec)
    assert any(
        "bottom missed 1500 pulse(s)" in p and "(1500 frame(s) behind" in p
        for p in result.problems
    )
    assert any("bottom: 1200 frame(s) exposed late" in w for w in result.warnings)
    bottom = result.cameras[1]
    assert bottom.missed_count == 1500 and bottom.late_count == 1200
    runner = CliRunner()
    data = json.loads(runner.invoke(app, ["check", "--json", str(rec)]).output)
    assert data[0]["cameras"][1]["missed_pulses"] == 1500
    assert data[0]["cameras"][1]["late_frames"] == 1200
    text = runner.invoke(app, ["check", str(rec)]).output
    assert "1500 missed pulse(s), 1200 late" in text


def test_external_misses_come_from_pulse_index_not_the_capped_list(tmp_path):
    # Every third pulse missed, 1500 in all (the last frame is pulse 4501);
    # nothing filled on an external trigger.
    pulses = np.asarray([p for p in range(4502) if p % 3 != 2], dtype=np.int64)
    missed = [p for p in range(4502) if p % 3 == 2]
    frames = len(pulses)
    rec = _write(
        tmp_path / "a",
        {"top": 10**15 + pulses * P},
        schema=4,
        extra_summary={
            "trigger_source": "external",
            "pulse_train": {"fill": False, "count": None},
        },
        arrays_extra={
            "top/missed": np.zeros(frames, dtype=bool),
            "top/dropped": np.zeros(frames, dtype=bool),
            "top/pulse_index": pulses,
        },
    )
    _patch_summary(
        rec,
        0,
        missed_pulses=1500,
        missed_pulse_indices=missed[:1000],
        late_frames=1200,
        late_pulse_indices=list(range(1000)),
    )
    result = check_recording(rec)
    assert any("(1500 frame(s) behind by the end)" in p for p in result.problems)
    assert any("top: 1200 frame(s) exposed late" in w for w in result.warnings)
    top = result.cameras[0]
    assert top.source == "recorded" and not top.filled
    assert top.missed == missed and top.missed_count == 1500
    # Pulse 2 falls after video frame 1 (pulses 0, 1), pulse 5 after frame 3, ...
    assert top.missed_after_frame[:3] == [1, 3, 5]
    assert top.missed_after_frame[-1] == frames - 3  # pulse 4499: 4500, 4501 follow


def test_an_early_stop_may_end_cameras_on_different_pulses(tmp_path):
    # Stopped by the operator: no fill to the train's count, so the cameras end
    # where their grab loops saw the stop (a Basler delivers sooner than a GS3).
    rec = _summary4(tmp_path / "stopped", [_cam4("top", 512), _cam4("bottom", 513)])
    result = check_recording(rec)
    assert result.ok, result.problems
    assert any(
        w.startswith("unequal frame counts (the recording was stopped before")
        for w in result.warnings
    )
    aborted = _summary4(
        tmp_path / "aborted", [_cam4("top", 1000), _cam4("bottom", 998)], aborted=True
    )
    assert check_recording(aborted).ok
    # A train that ran to its end pads every camera to its count: unequal is wrong.
    done = _summary4(tmp_path / "done", [_cam4("top", 1000), _cam4("bottom", 998)])
    result = check_recording(done)
    assert any(p.startswith("unequal frame counts: top 1000") for p in result.problems)


def test_the_recorders_completed_flag_decides_whether_a_take_ended_early(tmp_path):
    # The recorder says whether the take ran to its end; check believes it over
    # its frame-count guess (a stop right before the last pulse reaches the count).
    stopped = _summary4(
        tmp_path / "stopped",
        [_cam4("top", 1000), _cam4("bottom", 999)],
        completed=False,
    )
    assert check_recording(stopped).ok
    done = _summary4(
        tmp_path / "done", [_cam4("top", 512), _cam4("bottom", 513)], completed=True
    )
    result = check_recording(done)
    assert any(p.startswith("unequal frame counts: top 512") for p in result.problems)


def test_an_early_stop_on_an_external_trigger_is_judged_by_its_duration(tmp_path):
    early = _summary4(
        tmp_path / "early", [_cam4("top", 400), _cam4("bottom", 401)], fill=False
    )
    assert check_recording(early).ok
    full = _summary4(
        tmp_path / "full", [_cam4("top", 1000), _cam4("bottom", 999)], fill=False
    )
    assert not check_recording(full).ok


def test_a_start_the_recorder_left_unchecked_is_not_re_derived(tmp_path):
    # Schema 4, unlike cameras: the recorder compared no start offsets (None).
    # Timing events that would line these series up a pulse apart must not be
    # read into a start offset: the per-frame pulse_index is the recorder's word.
    events = {100: 900_000, 900: 1_500_000, 1500: 600_000}
    top = np.asarray(_train(2000, events=events), dtype=np.int64)
    bottom = np.asarray(_train(2001, events=events, first=1), dtype=np.int64)
    rec = _write(
        tmp_path / "unchecked",
        {"top": top, "bottom": bottom},
        schema=4,
        extra_summary={"pulse_train": {"fill": True, "count": 2000},
                       "sync": {"ok": True, "warnings": [], "notes": ["not compared"]}},
        arrays_extra={
            f"{name}/{key}": value
            for name in ("top", "bottom")
            for key, value in (
                ("missed", np.zeros(2000, dtype=bool)),
                ("dropped", np.zeros(2000, dtype=bool)),
                ("pulse_index", np.arange(2000)),
            )
        },
    )
    for index in (0, 1):
        _patch_summary(rec, index, **{**_cam4("x", 2000), "start_offset_pulses": None,
                                      "name": ("top", "bottom")[index]})
    result = check_recording(rec)
    assert not [p for p in result.problems if "offset" in p or "late" in p], result.problems


def test_recorded_and_rederived_start_offsets_share_a_sign(tmp_path):
    # bottom started one pulse late: its frame k is top's frame k+1.
    events = {100: 900_000, 900: 1_500_000, 1500: 600_000}
    top = _train(2000, events=events)
    bottom = _train(2001, events=events, first=1)
    old = check_recording(_write(tmp_path / "old", {"top": top, "bottom": bottom}))
    new = check_recording(
        _summary4(
            tmp_path / "new",
            [_cam4("top", 2000), _cam4("bottom", 2000, start_offset_pulses=1)],
        )
    )
    for result in (old, new):
        assert [c.start_offset for c in result.cameras] == [0, 1]
        assert [c.to_dict()["start_offset_pulses"] for c in result.cameras] == [0, 1]
    assert any(
        "bottom is offset from top" in p
        and "its frame k shows the moment of top frame k+1" in p
        for p in old.problems
    )


# ----------------------------------------------------------- host clock


def _host_deliveries(frames=1000, period=10_000_000, stall_every=200, stall=57_000_000):
    """Host delivery times of a camera that captured every pulse: ~2 ms after each
    exposure, and a 57 ms delivery stall every 200 frames that releases the frames
    it held back in a burst."""
    rng = np.random.default_rng(1)
    t0 = 1_758_000_000 * 10**9  # host wall clock (2025)
    out: list[int] = []
    held_until = 0
    for i in range(frames):
        t = t0 + i * period + 2_000_000 + int(rng.integers(0, 800_000))
        if i and i % stall_every == 0:
            held_until = t + stall
        if t < held_until:
            t = held_until + 50_000 * (len(out) % 8)
        out.append(max(t, out[-1] + 1) if out else t)
    return out


def test_host_clock_timestamps_are_not_rederived_into_missed_pulses(tmp_path):
    host = _host_deliveries()
    rec = _write(
        tmp_path / "a",
        {"top": host, "bottom": host},
        extra_summary={"fps_target": 100.0},
    )
    _patch_summary(rec, 0, timestamp_source="host")
    _patch_summary(rec, 1, timestamp_source="host")
    result = check_recording(rec)
    assert result.ok, result.problems
    assert [c.source for c in result.cameras] == ["none", "none"]
    assert any(
        "top: its timestamps are host delivery times" in w for w in result.warnings
    )


def test_wall_clock_timestamps_are_recognized_without_a_timestamp_source(tmp_path):
    rec = _write(
        tmp_path / "a", {"top": _host_deliveries()}, extra_summary={"fps_target": 100.0}
    )
    result = check_recording(rec)
    assert result.ok, result.problems
    assert any("look like host wall-clock times" in w for w in result.warnings)


# ------------------------------------------------------------ unreadable


def test_an_unreadable_recording_is_a_problem_and_the_scan_goes_on(tmp_path):
    good = _write(tmp_path / "1good", {"top": _train(500), "bottom": _train(500)})
    truncated = _write(tmp_path / "2npz", {"top": _train(500), "bottom": _train(500)})
    npz = truncated / "timestamps.npz"
    npz.write_bytes(npz.read_bytes()[: npz.stat().st_size // 2])
    empty = _write(tmp_path / "3empty", {"top": _train(500)})
    (empty / "timestamps.npz").write_bytes(b"")
    malformed = tmp_path / "4summary"
    malformed.mkdir()
    (malformed / "recording_summary.json").write_text("{not json")
    odd = tmp_path / "5content"
    odd.mkdir()
    (odd / "recording_summary.json").write_text(json.dumps({"cameras": ["top"]}))

    runner = CliRunner()
    as_json = runner.invoke(app, ["check", "--json", str(tmp_path)])
    assert as_json.exit_code == 1
    assert as_json.exception is None or isinstance(as_json.exception, SystemExit)
    data = {Path(r["folder"]).name: r for r in json.loads(as_json.output)}
    assert list(data) == ["1good", "2npz", "3empty", "4summary", "5content"]
    assert data["1good"]["ok"]
    for name in ("2npz", "3empty", "4summary", "5content"):
        assert not data[name]["ok"]
        assert data[name]["problems"][0].startswith("unreadable: "), data[name]
    assert "timestamps.npz" in data["2npz"]["problems"][0]
    assert "recording_summary.json" in data["4summary"]["problems"][0]
    # The folder whose npz broke is not told to enable record.save_timestamps.
    assert not any("save_timestamps" in w for w in data["2npz"]["warnings"])

    text = runner.invoke(app, ["check", str(tmp_path)])
    assert text.exit_code == 1
    assert "5 recording(s) checked" in text.output
    assert "4 with problems" in text.output
    assert check_recording(good).ok


def test_a_truncated_npz_still_reports_the_schema4_summary(tmp_path):
    rec = _summary4(
        tmp_path / "a",
        [
            _cam4("top", 1000),
            _cam4("bottom", 1000, missed_pulses=3, missed_pulse_indices=[5, 6, 7]),
        ],
    )
    (rec / "timestamps.npz").write_bytes(b"PK\x03\x04 truncated")
    result = check_recording(rec)
    assert result.problems[0].startswith("unreadable: timestamps.npz")
    assert result.cameras[1].missed_count == 3


# ------------------------------------------------- against the real recorder


@pytest.mark.parametrize("save_timestamps", [True, False])
def test_a_fake_recording_checks_the_same_with_or_without_timestamps(
    tmp_path, monkeypatch, save_timestamps
):
    # The summary and timestamps.npz the recorder really writes, not hand-built
    # ones: a filled miss is a warning either way.
    from octacam.cameras import CameraSystem
    from octacam.cameras.fake import FakeBackend
    from octacam.controller import RecordingController, RecordingSettings

    monkeypatch.setenv("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")
    system = CameraSystem(["FAKE-0", "FAKE-1"], backend="fake")
    try:
        system.load_config(tmp_path)
        for camera in system:
            camera.set_geometry(width=64, height=48)
        missing = next(c for c in system if c.serial_number == "FAKE-1").backend
        assert isinstance(missing, FakeBackend)
        missing.miss_triggers = {10, 30}
        settings = RecordingSettings(
            fps=50.0,
            duration_s=1.0,
            save_dir=str(tmp_path / "rec"),
            save_method="raw",
            save_frame_timestamps=save_timestamps,
            trigger_source="software",
        )
        controller = RecordingController(system, settings, auto_preview=False)
        assert controller.start_recording().ok
        controller.join(timeout=30)
    finally:
        system.close()
    assert (tmp_path / "rec" / "timestamps.npz").exists() == save_timestamps
    result = check_recording(tmp_path / "rec")
    assert result.ok, result.problems
    source = "recorded" if save_timestamps else "summary"
    assert [c.source for c in result.cameras] == [source, source]
    bad = next(c for c in result.cameras if c.name == "FAKE-1")
    assert bad.filled and bad.missed == [10, 30] and bad.missed_count == 2
    assert [c.start_offset for c in result.cameras] == [0, 0]
    assert result.warnings == [
        "FAKE-1 missed 2 pulse(s) (10, 30), filled with the previous frame: "
        "frames stay aligned"
    ]
