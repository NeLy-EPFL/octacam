"""`octacam transcode`: writer dispatch + CLI folder/file/summary resolution."""

import json
import logging
import os

os.environ.setdefault("PYLON_CAMEMU", "2")

import numpy as np
import pytest
from typer.testing import CliRunner

from octacam.cli import app
from octacam.transform import DisplayTransform
from octacam.writer import (
    TranscodeProgress,
    _parse_progress,
    _reporting_args,
    is_partial_transcode,
    transcode_encoded,
    transcode_file,
    transcode_raw,
)

runner = CliRunner()
cv2 = pytest.importorskip("cv2")


def _frame(width, height):
    return np.arange(height * width, dtype=np.uint8).reshape(height, width) * 2


def _write_raw(path, frame, fps=10.0):
    height, width = frame.shape
    path.write_bytes(frame.tobytes())
    path.with_suffix(".json").write_text(
        json.dumps(
            {"width": width, "height": height, "pixel_format": "Mono8", "fps": fps}
        )
    )


def _make_mkv(path, frame):
    """Encode a one-frame .mkv next to ``path`` (an encoded-input fixture)."""
    raw = path.with_suffix(".raw")
    _write_raw(raw, frame)
    transcode_raw(raw, crf=0, preset="ultrafast", output=path)
    raw.unlink()
    raw.with_suffix(".json").unlink()
    return path


def _dims(path):
    cap = cv2.VideoCapture(str(path))
    ok, frame = cap.read()
    cap.release()
    assert ok, path
    return frame.shape[1], frame.shape[0]  # (width, height)


def _summary(folder, cameras):
    (folder / "recording_summary.json").write_text(
        json.dumps({"schema_version": 1, "cameras": cameras})
    )


# ------------------------------------------------------------------ writer


def test_transcode_file_raw_to_mp4(tmp_path):
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))
    out = transcode_file(raw, tmp_path / "cam.mp4")
    assert out.suffix == ".mp4" and out.exists()
    assert _dims(out) == (16, 12)


def test_transcode_encoded_produces_valid_video(tmp_path):
    src = _make_mkv(tmp_path / "cam.mkv", _frame(16, 12))
    out = transcode_encoded(src, tmp_path / "cam.mp4")  # mkv -> re-encoded mp4
    assert _dims(out) == (16, 12)


def test_transcode_encoded_always_reencodes_never_copies(tmp_path, monkeypatch):
    # An already-encoded source must be re-encoded with the chosen slow preset,
    # not stream-copied: capture uses a fast preset, so this offline pass is
    # where the compression is earned. Regression guard for the dropped
    # `-c copy` shortcut that made `transcode` a near-instant remux.
    captured = {}

    def fake_run(args, src, **kwargs):
        captured["args"] = args
        open(args[-1], "wb").close()  # a real run leaves the output file in place

    monkeypatch.setattr("octacam.writer._run_ffmpeg", fake_run)
    transcode_encoded(
        tmp_path / "cam.mkv", tmp_path / "cam.mp4", crf=20, preset="veryslow"
    )
    args = captured["args"]
    assert "copy" not in args  # no stream-copy shortcut
    assert "libx264" in args
    assert args[args.index("-preset") + 1] == "veryslow"
    assert args[args.index("-crf") + 1] == "20"
    assert args[args.index("-pix_fmt") + 1] == "gray"


def test_transcode_file_applies_vf(tmp_path):
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))
    from octacam.transform import display_vf_filter

    out = transcode_file(
        raw, tmp_path / "cam.mp4", vf=display_vf_filter(DisplayTransform(90))
    )
    assert _dims(out) == (12, 16)  # 90deg swap


# ------------------------------------------------------- progress reporting


def test_reporting_args_octacam_adds_progress_stream():
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", "x.raw", "out.mkv"]
    out = _reporting_args(args, raw_output=False)
    assert out[0] == "ffmpeg"
    assert out[out.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in out
    assert out.count("-loglevel") == 1
    assert out[out.index("-loglevel") + 1] == "warning"
    # The core encode args survive untouched.
    assert out[out.index("-i") + 1] == "x.raw" and out[-1] == "out.mkv"


def test_reporting_args_ffmpeg_mode_streams_native_stats():
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", "x.raw", "out.mkv"]
    out = _reporting_args(args, raw_output=True)
    assert "-progress" not in out  # no machine-readable stream in raw mode
    assert "-stats" in out
    assert out[out.index("-loglevel") + 1] == "info"


def test_reporting_args_replaces_prebaked_flags_without_duplication():
    # Pre-existing reporting flags must be stripped, not duplicated.
    args = [
        "ffmpeg",
        "-nostats",
        "-progress",
        "pipe:1",
        "-loglevel",
        "warning",
        "-i",
        "x",
        "out",
    ]
    out = _reporting_args(args, raw_output=True)
    assert out.count("-progress") == 0
    assert out.count("-loglevel") == 1
    assert out.count("-nostats") == 0 and out.count("-stats") == 1


def test_parse_progress_emits_one_sample_per_block():
    lines = [
        "frame=10\n",
        "fps=N/A\n",
        "speed=N/A\n",
        "out_time_us=N/A\n",
        "progress=continue\n",
        "frame=50\n",
        "fps=25.0\n",
        "speed=2.0x\n",
        "out_time_us=2000000\n",
        "progress=end\n",
    ]
    samples: list[TranscodeProgress] = []
    _parse_progress(iter(lines), samples.append, total_frames=50)
    assert len(samples) == 2
    first, last = samples
    # N/A fields keep their default (0.0) rather than crashing.
    assert (first.frame, first.fps, first.speed, first.done) == (10, 0.0, 0.0, False)
    assert (last.frame, last.fps, last.speed) == (50, 25.0, 2.0)
    assert last.out_time_s == 2.0 and last.done and last.total_frames == 50


def test_transcode_raw_reports_progress_with_exact_total(tmp_path):
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))  # exactly one 16x12 Mono8 frame
    samples: list[TranscodeProgress] = []
    transcode_raw(
        raw,
        crf=0,
        preset="ultrafast",
        output=tmp_path / "cam.mkv",
        on_progress=samples.append,
    )
    assert samples, "expected at least the terminal progress sample"
    last = samples[-1]
    assert last.done and last.total_frames == 1 and last.frame == 1


def test_transcode_raw_output_mode_still_produces_file(tmp_path):
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))
    out = transcode_file(raw, tmp_path / "cam.mp4", raw_output=True)
    assert out.exists() and _dims(out) == (16, 12)


def test_transcode_raw_propagates_and_recovers_from_callback_error(tmp_path):
    # A raising progress callback must propagate (and the ffmpeg child is killed
    # and reaped on the way out — the regression guard for the lost cleanup).
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))

    class Boom(Exception):
        pass

    def boom(_p):
        raise Boom

    with pytest.raises(Boom):
        transcode_raw(
            raw,
            crf=0,
            preset="ultrafast",
            output=tmp_path / "cam.mkv",
            on_progress=boom,
        )


class _Stop(BaseException):
    """Stand-in for KeyboardInterrupt: a BaseException raised mid-encode."""


def _raise_stop(_p):
    raise _Stop


def test_interrupt_leaves_no_partial_output(tmp_path):
    # A Ctrl-C (modelled by a BaseException-raising callback) mid-encode must
    # leave the output path empty and no temp file behind: the encode writes a
    # discardable sibling, never a truncated file masquerading as finished.
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(64, 48))
    out = tmp_path / "cam.mkv"
    with pytest.raises(_Stop):
        transcode_raw(raw, crf=0, preset="ultrafast", output=out, on_progress=_raise_stop)
    assert not out.exists()  # no partial final file
    leftovers = [p.name for p in tmp_path.iterdir() if p.name not in ("cam.raw", "cam.json")]
    assert leftovers == [], f"orphaned temp files: {leftovers}"


def test_interrupt_does_not_clobber_existing_output(tmp_path):
    # An interrupted re-transcode must not destroy a previously good output: the
    # new file is renamed into place only once whole, so the old one survives.
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(64, 48))
    out = tmp_path / "cam.mkv"
    out.write_bytes(b"PREEXISTING-GOOD-OUTPUT")
    with pytest.raises(_Stop):
        transcode_raw(raw, crf=0, preset="ultrafast", output=out, on_progress=_raise_stop)
    assert out.read_bytes() == b"PREEXISTING-GOOD-OUTPUT"  # untouched


def test_interrupt_leaves_no_partial_output_encoded(tmp_path):
    # transcode_encoded (re-encode of an already-encoded source) shares
    # _atomic_output, so an interrupt mid-re-encode must also leave no output
    # and no temp behind.
    src = _make_mkv(tmp_path / "cam.mkv", _frame(64, 48))
    out = tmp_path / "out.mp4"
    with pytest.raises(_Stop):
        transcode_encoded(src, out, crf=0, preset="ultrafast", on_progress=_raise_stop)
    assert not out.exists()
    assert src.exists()  # the source is never touched on the way out
    assert not any(is_partial_transcode(p) for p in tmp_path.iterdir())


def test_successful_transcode_leaves_no_temp_file(tmp_path):
    # The happy path must not strand the temp sibling either.
    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))
    transcode_raw(raw, crf=0, preset="ultrafast", output=tmp_path / "cam.mkv")
    assert (tmp_path / "cam.mkv").exists()
    assert not any(is_partial_transcode(p) for p in tmp_path.iterdir())


def test_progress_bar_indeterminate_after_determinate(tmp_path):
    # A file with a known total followed by one without must NOT inherit the
    # prior total (rich's reset/update keep total on None) — regression guard.
    from octacam.cli import _TranscodeProgressBar

    bar = _TranscodeProgressBar(2)
    determinate = bar.file(1, tmp_path / "a.raw")
    determinate(TranscodeProgress(50, 10.0, 5.0, 1.0, total_frames=100, done=False))
    assert bar._progress.tasks[-1].total == 100

    indeterminate = bar.file(2, tmp_path / "b.mkv")
    indeterminate(TranscodeProgress(30, 10.0, 3.0, 1.0, total_frames=None, done=False))
    assert bar._progress.tasks[-1].total is None  # not the stale 100
    assert len(bar._progress.tasks) == 1  # only one bar is kept visible


def test_progress_bar_snaps_to_full_when_total_overshoots(tmp_path):
    # The frame total is only a hint; a recording with dropped frames encodes
    # fewer than the hint, so the final block must still read 100%.
    from octacam.cli import _TranscodeProgressBar

    bar = _TranscodeProgressBar(1)
    on_progress = bar.file(1, tmp_path / "a.raw")
    on_progress(TranscodeProgress(90, 10.0, 9.0, 1.0, total_frames=100, done=True))
    task = bar._progress.tasks[-1]
    assert task.completed >= task.total  # bar reaches 100% despite the overshoot
    assert task.finished  # finished => a solid full bar, not a partial one


def test_progress_bar_snaps_to_full_when_total_undershoots(tmp_path):
    # The hint can also undershoot (more frames encoded than expected); the bar
    # must still land on a full 100% rather than appearing to overflow.
    from octacam.cli import _TranscodeProgressBar

    bar = _TranscodeProgressBar(1)
    on_progress = bar.file(1, tmp_path / "a.raw")
    on_progress(TranscodeProgress(110, 10.0, 11.0, 1.0, total_frames=100, done=True))
    task = bar._progress.tasks[-1]
    assert task.total == 110 and task.completed == 110 and task.finished


def test_progress_bar_indeterminate_snaps_to_full_when_done(tmp_path):
    # A file with no known total draws an indeterminate bar; on completion it
    # must still close on a clean 100% (final frame count becomes the total)
    # instead of vanishing mid-pulse — the "moved on before 100%" symptom.
    from octacam.cli import _TranscodeProgressBar

    bar = _TranscodeProgressBar(1)
    on_progress = bar.file(1, tmp_path / "a.mkv")
    on_progress(TranscodeProgress(40, 10.0, 4.0, 1.0, total_frames=None, done=False))
    assert bar._progress.tasks[-1].total is None  # indeterminate while running
    on_progress(TranscodeProgress(42, 10.0, 4.2, 1.0, total_frames=None, done=True))
    task = bar._progress.tasks[-1]
    assert task.total == 42 and task.completed == 42 and task.finished


# --------------------------------------------------------------------- CLI


def _run(*args):
    return runner.invoke(app, ["transcode", *args])


# ----------------------------------------------- cache-driven selectors


@pytest.fixture(autouse=True)
def cache_env(tmp_path, monkeypatch):
    """Isolate the recording cache (session_cache) under a throwaway dir.

    Autouse so even plain-path transcodes (which publish a transcode-activity
    marker) never write to the real ~/.cache/octacam during tests."""
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))


def _recording_folder(tmp_path, name, session="s1"):
    """Create a one-camera recording folder and note it in the cache."""
    from octacam import session_cache

    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    _write_raw(folder / "cam0.raw", _frame(16, 12))
    _summary(
        folder,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform().to_dict(),
                "transform_applied": True,
            }
        ],
    )
    session_cache.record_recording(folder, session)
    return folder


def test_transcode_last_uses_cache(tmp_path, cache_env):
    f1 = _recording_folder(tmp_path, "rec1")
    f2 = _recording_folder(tmp_path, "rec2")
    result = _run("--last")
    assert result.exit_code == 0, result.output
    assert (f2 / "cam0.mp4").exists()  # only the most recent folder
    assert not (f1 / "cam0.mp4").exists()


def test_transcode_session_uses_cache(tmp_path, cache_env):
    f_old = _recording_folder(tmp_path, "old", session="s1")
    f1 = _recording_folder(tmp_path, "rec1", session="s2")
    f2 = _recording_folder(tmp_path, "rec2", session="s2")
    result = _run("--session")
    assert result.exit_code == 0, result.output
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()
    assert not (f_old / "cam0.mp4").exists()  # an earlier session is excluded


def test_transcode_session_ignores_deleted_folder(tmp_path, cache_env):
    import shutil

    f1 = _recording_folder(tmp_path, "rec1", session="s1")
    f2 = _recording_folder(tmp_path, "rec2", session="s1")
    shutil.rmtree(f1)  # removed between recording and transcoding -> ignored
    result = _run("--session")
    assert result.exit_code == 0, result.output
    assert (f2 / "cam0.mp4").exists()


def test_transcode_session_id_targets_exact_session(tmp_path, cache_env):
    # --session-id names one exact session, unaffected by a later recording that
    # would steal the "latest session" out from under bare --session.
    f1 = _recording_folder(tmp_path, "rec1", session="guiA")
    f2 = _recording_folder(tmp_path, "rec2", session="guiA")
    later = _recording_folder(tmp_path, "rec3", session="recB")  # a later session
    result = _run("--session-id", "guiA")
    assert result.exit_code == 0, result.output
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()
    assert not (later / "cam0.mp4").exists()
    # Bare --session would instead pick the later session (regression guard).
    for folder in (f1, f2, later):
        (folder / "cam0.mp4").unlink(missing_ok=True)
    assert _run("--session").exit_code == 0
    assert (later / "cam0.mp4").exists()
    assert not (f1 / "cam0.mp4").exists()


def test_transcode_all_uses_cache_across_sessions(tmp_path, cache_env):
    f_old = _recording_folder(tmp_path, "old", session="s1")
    f1 = _recording_folder(tmp_path, "rec1", session="s2")
    f2 = _recording_folder(tmp_path, "rec2", session="s3")
    result = _run("--all")
    assert result.exit_code == 0, result.output
    # Every folder in the cache, regardless of session or day.
    assert (f_old / "cam0.mp4").exists()
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()


def test_transcode_all_empty_cache_errors(tmp_path, cache_env):
    result = _run("--all")
    assert result.exit_code != 0
    assert "No recordings found" in result.output


def test_transcode_selectors_are_mutually_exclusive(tmp_path, cache_env):
    _recording_folder(tmp_path, "rec1")
    result = _run("--last", "--session")
    assert result.exit_code != 0
    assert "at most one" in result.output
    # --all is part of the mutual-exclusion set too.
    result = _run("--all", "--last")
    assert result.exit_code != 0
    assert "at most one" in result.output


def test_transcode_selector_rejects_explicit_paths(tmp_path, cache_env):
    f1 = _recording_folder(tmp_path, "rec1")
    result = _run(str(f1), "--last")
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_transcode_selector_empty_cache_errors(tmp_path, cache_env):
    result = _run("--last")
    assert result.exit_code != 0
    assert "No recordings found" in result.output


def test_folder_with_summary_as_saved_keeps_orientation(tmp_path):
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    _summary(
        tmp_path,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform(90).to_dict(),
                "transform_applied": False,
            }
        ],
    )
    result = _run(str(tmp_path))
    assert result.exit_code == 0, result.output
    assert _dims(tmp_path / "cam0.mp4") == (16, 12)  # as-saved: no transform


def test_folder_with_summary_as_displayed_applies_transform(tmp_path):
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    _summary(
        tmp_path,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform(90).to_dict(),
                "transform_applied": False,
            }
        ],
    )
    result = _run(str(tmp_path), "--as-displayed")
    assert result.exit_code == 0, result.output
    assert _dims(tmp_path / "cam0.mp4") == (12, 16)  # rotated


def test_as_displayed_does_not_reapply_when_already_baked(tmp_path):
    # Display-form recording: the raw is already rotated (12x16) and flagged.
    _write_raw(tmp_path / "cam0.raw", _frame(12, 16))
    _summary(
        tmp_path,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform(90).to_dict(),
                "transform_applied": True,
            }
        ],
    )
    result = _run(str(tmp_path), "--as-displayed")
    assert result.exit_code == 0, result.output
    assert _dims(tmp_path / "cam0.mp4") == (12, 16)  # unchanged, not re-rotated


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_folder_without_summary_resolves_plain_jobs_and_warns(tmp_path):
    from octacam.cli import _transcode_jobs

    _write_raw(tmp_path / "a.raw", _frame(16, 12))
    _make_mkv(tmp_path / "b.mkv", _frame(16, 12))
    # The CLI callback clears the octacam logger's handlers, so exercise the
    # resolver directly to capture its warning and inspect the jobs.
    handler = _ListHandler()
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        jobs = _transcode_jobs(
            [tmp_path], recursive=False, as_displayed=False, out_format="mp4"
        )
    finally:
        logger.removeHandler(handler)
    assert sorted(p.name for p, _vf, _n in jobs) == ["a.raw", "b.mkv"]
    assert all(vf == "" for _p, vf, _n in jobs)  # no transform without a summary
    assert any("recording_summary.json" in m for m in handler.messages)


def test_folder_without_summary_transcodes_to_mp4(tmp_path):
    _write_raw(tmp_path / "a.raw", _frame(16, 12))
    _make_mkv(tmp_path / "b.mkv", _frame(16, 12))
    result = _run(str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "a.mp4").exists()
    assert (tmp_path / "b.mp4").exists()


def test_summary_skips_zero_frame_cameras_with_warning(tmp_path):
    from octacam.cli import _transcode_jobs

    # A real capture and a 0-frame (header-only) capture in the same folder.
    _make_mkv(tmp_path / "good.mkv", _frame(16, 12))
    (tmp_path / "empty.mkv").write_bytes(b"\x00" * 64)  # header-only stub
    _summary(
        tmp_path,
        [
            {"file": "good.mkv", "frames": 120},
            {"file": "empty.mkv", "frames": 0},
        ],
    )
    handler = _ListHandler()
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        jobs = _transcode_jobs(
            [tmp_path], recursive=False, as_displayed=False, out_format="mp4"
        )
    finally:
        logger.removeHandler(handler)
    # The frameless file is skipped; the real one is still queued.
    assert [p.name for p, _vf, _n in jobs] == ["good.mkv"]
    assert any("0 frames" in m for m in handler.messages)


def test_frameless_folder_transcodes_cleanly_without_error(tmp_path):
    # A folder whose only camera captured 0 frames must not be handed to ffmpeg
    # (which would emit a cryptic matroska error and a non-zero exit); it is
    # skipped, leaving the run successful with nothing transcoded.
    (tmp_path / "cam0.mkv").write_bytes(b"\x00" * 64)
    _summary(tmp_path, [{"file": "cam0.mkv", "frames": 0}])
    result = _run(str(tmp_path))
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "cam0.mp4").exists()


def test_single_file_uses_summary_in_its_folder(tmp_path):
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    _summary(
        tmp_path,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform(90).to_dict(),
                "transform_applied": False,
            }
        ],
    )
    result = _run(
        str(tmp_path / "cam0.raw"), "--as-displayed"    )
    assert result.exit_code == 0, result.output
    assert _dims(tmp_path / "cam0.mp4") == (12, 16)


def test_recursive_and_mixed_args_dedup(tmp_path):
    sub_a = tmp_path / "a"
    sub_b = tmp_path / "b"
    sub_a.mkdir()
    sub_b.mkdir()
    _write_raw(sub_a / "cam.raw", _frame(16, 12))
    _write_raw(sub_b / "cam.raw", _frame(16, 12))
    # Pass the parent recursively AND sub_b/cam.raw explicitly: the duplicate
    # must collapse to a single job (no double transcode / error).
    result = _run(
        "-r", str(tmp_path), str(sub_b / "cam.raw")    )
    assert result.exit_code == 0, result.output
    assert (sub_a / "cam.mp4").exists()
    assert (sub_b / "cam.mp4").exists()
    assert result.output.count(str(sub_b / "cam.mp4")) == 1


def test_remove_source_deletes_raw_and_sidecar_keeps_summary(tmp_path):
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    _summary(
        tmp_path,
        [
            {
                "file": "cam0.raw",
                "transform": DisplayTransform().to_dict(),
                "transform_applied": True,
            }
        ],
    )
    result = _run(str(tmp_path), "--remove-source")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam0.mp4").exists()
    assert not (tmp_path / "cam0.raw").exists()
    assert not (tmp_path / "cam0.json").exists()  # raw geometry sidecar removed
    assert (tmp_path / "recording_summary.json").exists()  # summary kept


def test_remove_source_deletes_mkv(tmp_path):
    _make_mkv(tmp_path / "cam.mkv", _frame(16, 12))
    result = _run(str(tmp_path), "--remove-source")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam.mp4").exists()
    assert not (tmp_path / "cam.mkv").exists()


def test_remove_source_keeps_file_when_transcode_fails(tmp_path):
    # A .raw with no .json sidecar fails to transcode; the source must survive.
    (tmp_path / "orphan.raw").write_bytes(_frame(16, 12).tobytes())
    result = _run(str(tmp_path), "--remove-source")
    assert result.exit_code != 0
    assert (tmp_path / "orphan.raw").exists()


def test_transcode_default_format_and_override(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_raw(rec / "cam.raw", _frame(16, 12))
    # The default container is mp4 (the CLI default; no config involved).
    assert _run(str(rec)).exit_code == 0
    assert (rec / "cam.mp4").exists()
    # CLI --format overrides the default.
    (rec / "cam2.raw").write_bytes((rec / "cam.raw").read_bytes())
    (rec / "cam2.json").write_text((rec / "cam.json").read_text())
    assert _run(str(rec / "cam2.raw"), "--format", "mkv").exit_code == 0
    assert (rec / "cam2.mkv").exists()


def test_transcode_cli_ffmpeg_progress_style(tmp_path):
    # --progress-style ffmpeg streams ffmpeg's native output but still succeeds.
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    result = _run(
        str(tmp_path), "--progress-style", "ffmpeg"
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam0.mp4").exists()


def test_transcode_cli_octacam_progress_style_is_default(tmp_path):
    # The default style transcodes cleanly (the bar is a no-op off a terminal).
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    result = _run(str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam0.mp4").exists()
    assert str(tmp_path / "cam0.mp4") in result.output  # result path on stdout


def test_transcode_cli_keyboardinterrupt_stops_gracefully(tmp_path, monkeypatch):
    # Ctrl-C mid-batch through the REAL transcode_file/_atomic_output path: stop
    # cleanly with the SIGINT exit code, keep the finished file's renamed output,
    # and leave the interrupted file with neither a final nor a temp artifact.
    import octacam.writer as writer

    _write_raw(tmp_path / "a.raw", _frame(16, 12))
    _write_raw(tmp_path / "b.raw", _frame(16, 12))
    calls: list = []

    def fake_run_ffmpeg(args, src, **kwargs):
        calls.append(src)
        open(args[-1], "wb").close()  # ffmpeg writes the temp output...
        if len(calls) == 2:
            raise KeyboardInterrupt  # ...then Ctrl-C lands during the 2nd file

    monkeypatch.setattr(writer, "_run_ffmpeg", fake_run_ffmpeg)
    result = _run(str(tmp_path))
    assert result.exit_code == 130, result.output  # 128 + SIGINT
    assert len(calls) == 2  # stopped at the interrupted file, no further jobs
    assert (tmp_path / "a.mp4").exists()  # finished output renamed into place
    assert not (tmp_path / "b.mp4").exists()  # interrupted output absent
    # the interrupted encode's temp is discarded, not orphaned
    assert not any(is_partial_transcode(p) for p in tmp_path.iterdir())


def test_raw_output_interrupt_cleans_temp(tmp_path, monkeypatch):
    # The --progress-style ffmpeg path runs ffmpeg via subprocess.run (not Popen);
    # a Ctrl-C there must still discard the partial temp — the cleanup lives in
    # _atomic_output, wrapping both progress modes.
    import octacam.writer as writer

    raw = tmp_path / "cam.raw"
    _write_raw(raw, _frame(16, 12))

    def fake_run(args, *a, **k):
        open(args[-1], "wb").close()  # ffmpeg wrote a partial file...
        raise KeyboardInterrupt  # ...then the terminal's SIGINT arrives

    monkeypatch.setattr(writer.subprocess, "run", fake_run)
    with pytest.raises(KeyboardInterrupt):
        transcode_raw(
            raw, crf=0, preset="ultrafast", output=tmp_path / "cam.mkv", raw_output=True
        )
    assert not (tmp_path / "cam.mkv").exists()
    assert not any(is_partial_transcode(p) for p in tmp_path.iterdir())


def test_transcode_skips_orphaned_partial_files(tmp_path):
    # A partial temp file a hard kill orphaned must not be picked up as a job.
    from octacam.cli import _transcode_jobs
    from octacam.writer import _partial_path

    _write_raw(tmp_path / "cam.raw", _frame(16, 12))
    orphan = _partial_path(tmp_path / "cam.mkv")
    orphan.write_bytes(b"\x00" * 32)  # leftover ".octacam-part" sibling
    assert is_partial_transcode(orphan)
    # ...whether discovered by a folder scan...
    jobs = _transcode_jobs(
        [tmp_path], recursive=False, as_displayed=False, out_format="mkv"
    )
    assert sorted(p.name for p, _vf, _n in jobs) == ["cam.raw"]  # orphan skipped
    # ...or named explicitly on the command line.
    explicit = _transcode_jobs(
        [orphan], recursive=False, as_displayed=False, out_format="mkv"
    )
    assert explicit == []


def test_transcode_cli_rejects_unknown_progress_style(tmp_path):
    _write_raw(tmp_path / "cam0.raw", _frame(16, 12))
    result = _run(
        str(tmp_path), "--progress-style", "bogus"
    )
    assert result.exit_code != 0
