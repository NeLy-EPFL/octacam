"""`octacam check`: screening recordings for missed pulses and desync."""

import json

import numpy as np
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
    arrays = {f"{n}/timestamp_ns": np.asarray(t, dtype=np.int64) for n, t in cameras.items()}
    arrays.update(arrays_extra or {})
    np.savez_compressed(folder / "timestamps.npz", **arrays)
    return folder


def _train(n, missed=(), t0=10**15, events=None, first=0):
    events = events or {}
    return [t0 + p * P + events.get(p, 0) for p in range(first, n) if p not in missed]


def test_a_clean_pair_is_ok(tmp_path):
    rec = _write(tmp_path / "a", {"top": _train(2000), "bottom": _train(2000, t0=5 * 10**15)})
    result = check_recording(rec)
    assert result.ok, result.problems
    assert [c.missed for c in result.cameras] == [[], []]


def test_missed_pulses_and_unequal_counts_are_problems(tmp_path):
    rec = _write(
        tmp_path / "a", {"top": _train(2000, missed={665, 1283}), "bottom": _train(2000)}
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
    assert result.cameras[1].start_offset == 1
    assert any("offset from top from the start" in p for p in result.problems)


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
    assert data[0]["cameras"][0]["missed_pulses"] == [100]
