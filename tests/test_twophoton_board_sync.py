"""Aligning a take ThorSync did not record on the trigger board's clock: the
model, its fallbacks for a stopped-early capture, the sidecar and the render.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from octacam.cli import app
from octacam.plugins.twophoton.board_sync import (
    BOARD_SYNC_FILENAME,
    board_sync,
    camera_period_ms,
    edge_times,
    read_board_sync,
    read_board_take,
    run_period_ms,
    write_board_syncs,
)

runner = CliRunner()

# Real 2026-10-20 Fly3/001: 5000 frames, ThorImage stopped, synced at 100 fps.
STOP = {"reason": "thorimage", "edges": 5000, "high_ms": 323156, "run_ms": 328940}


def _take(
    root: Path,
    name: str,
    *,
    stop: dict,
    timepoints: int = 5000,
    rate: float = 15.312,
    thorsync: bool = False,
    frames: int = 32894,
) -> Path:
    """A transferred take: its info sidecars and its paired ThorImage folder."""
    take = root / "Fly3" / name
    info = take / "octacam_recording"
    info.mkdir(parents=True)
    (info / "recording_summary.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "fps_target": 100.0,
                "cameras": [{"name": "camera_RF", "frames": frames}],
            }
        )
    )
    (info / "twophoton.json").write_text(
        json.dumps({"armed": True, "stop_idle_ms": 2000, "stop": stop})
    )
    matched = [{"path": f"AllPAMxCI80/{name}_img", "kind": "image"}]
    if thorsync:
        matched.append({"path": "AllPAMxCI80/SyncData001", "kind": "sync"})
    (info / "twophoton_match.json").write_text(json.dumps({"matched": matched}))
    image = take / "2P" / f"{name}_img"
    image.mkdir(parents=True)
    (image / "Experiment.xml").write_text(
        f'<?xml version="1.0"?><ThorImageExperiment><LSM frameRate="{rate}" />'
        f'<Streaming enable="1" /><Timelapse timepoints="{timepoints}" />'
        "</ThorImageExperiment>"
    )
    return take


def test_the_camera_period_follows_the_firmware_s_timer():
    assert camera_period_ms(100) == 10.0
    assert camera_period_ms(1) == 1000.0
    # /64 is the first prescaler that fits: 17857 whole ticks per half period,
    # not 1000/7 ms.
    assert camera_period_ms(7) == 2 * 17857 * 64 / 16_000


def test_the_run_s_period_ends_one_high_time_past_the_last_frame():
    period = run_period_ms(STOP, 2000)
    assert period == pytest.approx((326940 - 323156 / 5000) / 4999)
    assert period == pytest.approx(65.388, abs=1e-3)
    assert run_period_ms({**STOP, "edges": 1}, 2000) is None


def test_a_completed_capture_uses_its_own_period(tmp_path):
    board = read_board_take(_take(tmp_path, "001", stop=STOP))
    assert board is not None and board.completed and not board.has_thorsync
    model = board_sync(board, [])
    assert model is not None
    assert model["twop_period_from"] == "this take"
    assert model["twop_period_ms"] == pytest.approx(65.388, abs=1e-3)
    assert model["camera_period_ms"] == 10.0
    assert model["first_camera_pulse_ms"] == 7.5
    assert model["start_uncertainty_ms"] == 2.5
    cameras, frames = edge_times(model)
    assert len(cameras) == 32894 and len(frames) == 5000
    assert frames[0] == 0.0 and cameras[0] == pytest.approx(0.0075)
    assert frames[-1] == pytest.approx(4999 * model["twop_period_ms"] / 1000)


def test_a_stopped_early_capture_borrows_the_session_s_period(tmp_path):
    # Real 2026-10-16: a stopped capture's own run is up to 250 frames off.
    full = _take(tmp_path, "001", stop=STOP, thorsync=True)
    stopped = {"reason": "thorimage", "edges": 1142, "high_ms": 75148, "run_ms": 78005}
    short = _take(tmp_path, "002", stop=stopped, frames=7800)
    other_rate = _take(tmp_path, "003", stop=STOP, rate=7.5)
    boards = [b for t in (full, short, other_rate) if (b := read_board_take(t))]
    model = board_sync(boards[1], boards)
    assert model is not None
    assert model["twop_period_from"].startswith(
        "median of 1 completed take"
    )  # not the 7.5 Hz one
    assert model["twop_period_ms"] == pytest.approx(65.388, abs=1e-3)
    # Alone, it falls back on ThorImage's nominal rate.
    alone = board_sync(boards[1], [boards[1]])
    assert alone is not None
    assert alone["twop_period_ms"] == pytest.approx(1000 / 15.312)
    assert "nominal" in alone["twop_period_from"]


def test_only_takes_without_thorsync_get_the_sidecar(tmp_path):
    with_sync = _take(tmp_path, "001", stop=STOP, thorsync=True)
    without = _take(tmp_path, "002", stop=STOP)
    assert write_board_syncs([with_sync, without], dry_run=True) == [without]
    assert read_board_sync(without) is None  # a dry run writes nothing
    assert write_board_syncs([with_sync, without]) == [without]
    assert read_board_sync(with_sync) is None
    model = read_board_sync(without)
    assert model is not None and model["source"] == "board"
    assert (without / "octacam_recording" / BOARD_SYNC_FILENAME).is_file()


def _unreported(take: Path) -> None:
    """Make *take* one from a board too old to send its end-of-run report."""
    (take / "octacam_recording" / "twophoton.json").write_text(
        json.dumps({"armed": True})
    )


def test_a_take_without_a_board_report_needs_a_rig_factor(tmp_path):
    take = _take(tmp_path, "001", stop=STOP)
    _unreported(take)
    board = read_board_take(take)
    assert board is not None and board.stop is None and not board.completed
    assert write_board_syncs([take]) == []  # nothing measures this rig's scans


def test_an_unreported_take_uses_the_rig_factor_of_its_scan_configuration(tmp_path):
    # Real 260903: no ThorSync and no board report; other days measured the
    # board's 2P period against ThorImage's nominal rate for the same scans.
    old = _take(tmp_path / "260903", "001", stop=STOP, timepoints=1000, frames=12900)
    _unreported(old)
    ref = _take(tmp_path / "261016", "001", stop=STOP)  # completed, reported
    other = _take(tmp_path / "261007", "001", stop=STOP, rate=7.7)
    for take, size in ((old, "1024x512"), (ref, "1024x512"), (other, "1024x1024")):
        xml = next((take / "2P").glob("*/Experiment.xml"))
        x, y = size.split("x")
        xml.write_text(
            xml.read_text().replace("<LSM ", f'<LSM pixelX="{x}" pixelY="{y}" ')
        )
    boards = [b for t in (old, ref, other) if (b := read_board_take(t))]
    model = board_sync(boards[0], boards)
    assert model is not None
    factor = run_period_ms(STOP, 2000) / (1000 / 15.312)
    assert factor == pytest.approx(1.00121, abs=2e-5)
    assert model["twop_period_ms"] == pytest.approx(1000 / 15.312 * factor)
    assert "this rig's factor for 1024x512 scans" in model["twop_period_from"]
    assert model["twop_pulses"] == 1000  # ThorImage's own count: no board count
    assert model["camera_pulses"] == 12900
    assert model["start_uncertainty_ms"] == 2.5  # an old board's phase
    assert write_board_syncs([old], references=[ref, other]) == [old]


def test_the_board_sync_command_backfills_a_day(tmp_path):
    take = _take(tmp_path / "261020_AllPAMxCI80", "001", stop=STOP)
    result = runner.invoke(app, ["twophoton", "board-sync", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert read_board_sync(take) is not None


def test_the_synced_render_reads_the_board_model(tmp_path):
    from octacam.plugins.twophoton.render import find_synced_inputs

    take = _take(tmp_path, "001", stop=STOP)
    (take / "grid.mp4").write_bytes(b"x")
    assert isinstance(find_synced_inputs(take), str)  # no ThorSync, no model
    write_board_syncs([take])
    inputs = find_synced_inputs(take)
    assert not isinstance(inputs, str)
    assert inputs.sync_dir is None
    assert inputs.board == read_board_sync(take)


def test_a_current_board_starts_the_cameras_half_a_period_in(tmp_path):
    # The 2026-10-21 firmware resets its pin toggle at each run's start.
    take = _take(tmp_path, "001", stop=STOP)
    info = take / "octacam_recording" / "twophoton.json"
    data = json.loads(info.read_text())
    info.write_text(json.dumps({**data, "camera_phase": "half_period"}))
    board = read_board_take(take)
    assert board is not None and board.half_period_phase
    model = board_sync(board, [])
    assert model is not None
    assert model["first_camera_pulse_ms"] == 5.0
    assert model["start_uncertainty_ms"] == 0.1


def _with_info(take: Path, **fields) -> None:
    info = take / "octacam_recording" / "twophoton.json"
    info.write_text(json.dumps({**json.loads(info.read_text()), **fields}))


@pytest.mark.parametrize(
    ("banner", "pulses", "half"),
    [
        ("2PHOTON 2 dcba4c14", None, True),  # the 2026-10-21 build
        ("2PHOTON 2 a1a23e47", 32894, True),  # a board that counts its pulses
        ("2PHOTON 2 0ldbu1ld", None, False),
        (None, None, False),
    ],
)
def test_the_phase_comes_from_what_the_board_said(tmp_path, banner, pulses, half):
    # Tommy's review: decided here from the build, not noted by the plugin
    # only while the board matched the source exactly.
    stop = STOP if pulses is None else {**STOP, "pulses": pulses}
    take = _take(tmp_path, "001", stop=stop)
    _with_info(take, firmware=banner)
    board = read_board_take(take)
    assert board is not None and board.half_period_phase is half


def test_a_video_that_misses_the_board_s_first_pulses_widens_the_start(tmp_path):
    # Tommy's review: an external take cannot be primed, so a camera that
    # ignores the first pulse makes frame 0 the board's second; the model must
    # not still claim 0.1 ms. The board counted 32896 pulses, the video 32894.
    take = _take(tmp_path, "001", stop={**STOP, "pulses": 32896})
    board = read_board_take(take)
    assert board is not None and board.board_pulses == 32896
    model = board_sync(board, [])
    assert model is not None
    assert model["start_uncertainty_ms"] == 20.0  # two 10 ms camera frames
    assert "32896 camera pulses" in model["warning"]
    # Counted and matching: the precise start, no warning.
    take = _take(tmp_path / "ok", "001", stop={**STOP, "pulses": 32894})
    model = board_sync(read_board_take(take), [])
    assert model is not None
    assert model["start_uncertainty_ms"] == 0.1 and "warning" not in model


def test_a_board_without_a_count_is_checked_against_its_run_time(tmp_path):
    # The 2026-10-21 build sends no count, but its pulses start half a period
    # in, so the run time implies one (to within a pulse).
    take = _take(tmp_path, "001", stop=STOP, frames=32894)
    _with_info(take, firmware="2PHOTON 2 dcba4c14")
    model = board_sync(read_board_take(take), [])
    assert model is not None and "warning" not in model
    short = _take(tmp_path / "short", "001", stop=STOP, frames=32890)
    _with_info(short, firmware="2PHOTON 2 dcba4c14")
    model = board_sync(read_board_take(short), [])
    assert model is not None and model["start_uncertainty_ms"] == 40.0
