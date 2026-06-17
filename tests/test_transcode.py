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
from octacam.writer import transcode_encoded, transcode_file, transcode_raw

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

    def fake_run(args, src):
        captured["args"] = args

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
    result = _run("--last", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (f2 / "cam0.mp4").exists()  # only the most recent folder
    assert not (f1 / "cam0.mp4").exists()


def test_transcode_session_uses_cache(tmp_path, cache_env):
    f_old = _recording_folder(tmp_path, "old", session="s1")
    f1 = _recording_folder(tmp_path, "rec1", session="s2")
    f2 = _recording_folder(tmp_path, "rec2", session="s2")
    result = _run("--session", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()
    assert not (f_old / "cam0.mp4").exists()  # an earlier session is excluded


def test_transcode_today_uses_cache(tmp_path, cache_env):
    f1 = _recording_folder(tmp_path, "rec1", session="s1")
    f2 = _recording_folder(tmp_path, "rec2", session="s2")
    result = _run("--today", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()  # spans sessions, all of today


def test_transcode_session_ignores_deleted_folder(tmp_path, cache_env):
    import shutil

    f1 = _recording_folder(tmp_path, "rec1", session="s1")
    f2 = _recording_folder(tmp_path, "rec2", session="s1")
    shutil.rmtree(f1)  # removed between recording and transcoding -> ignored
    result = _run("--session", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (f2 / "cam0.mp4").exists()


def test_transcode_session_id_targets_exact_session(tmp_path, cache_env):
    # --session-id names one exact session, unaffected by a later recording that
    # would steal the "latest session" out from under bare --session.
    f1 = _recording_folder(tmp_path, "rec1", session="guiA")
    f2 = _recording_folder(tmp_path, "rec2", session="guiA")
    later = _recording_folder(tmp_path, "rec3", session="recB")  # a later session
    result = _run("--session-id", "guiA", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()
    assert not (later / "cam0.mp4").exists()
    # Bare --session would instead pick the later session (regression guard).
    for folder in (f1, f2, later):
        (folder / "cam0.mp4").unlink(missing_ok=True)
    assert _run("--session", "--config-dir", str(tmp_path)).exit_code == 0
    assert (later / "cam0.mp4").exists()
    assert not (f1 / "cam0.mp4").exists()


def test_transcode_all_uses_cache_across_sessions(tmp_path, cache_env):
    f_old = _recording_folder(tmp_path, "old", session="s1")
    f1 = _recording_folder(tmp_path, "rec1", session="s2")
    f2 = _recording_folder(tmp_path, "rec2", session="s3")
    result = _run("--all", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    # Every folder in the cache, regardless of session or day.
    assert (f_old / "cam0.mp4").exists()
    assert (f1 / "cam0.mp4").exists()
    assert (f2 / "cam0.mp4").exists()


def test_transcode_all_empty_cache_errors(tmp_path, cache_env):
    result = _run("--all", "--config-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "No recordings found" in result.output


def test_transcode_selectors_are_mutually_exclusive(tmp_path, cache_env):
    _recording_folder(tmp_path, "rec1")
    result = _run("--last", "--today", "--config-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "at most one" in result.output
    # --all is part of the mutual-exclusion set too.
    result = _run("--all", "--last", "--config-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "at most one" in result.output


def test_transcode_selector_rejects_explicit_paths(tmp_path, cache_env):
    f1 = _recording_folder(tmp_path, "rec1")
    result = _run(str(f1), "--last", "--config-dir", str(tmp_path))
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_transcode_selector_empty_cache_errors(tmp_path, cache_env):
    result = _run("--last", "--config-dir", str(tmp_path))
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
    result = _run(str(tmp_path), "--config-dir", str(tmp_path))
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
    result = _run(str(tmp_path), "--as-displayed", "--config-dir", str(tmp_path))
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
    result = _run(str(tmp_path), "--as-displayed", "--config-dir", str(tmp_path))
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
    assert sorted(p.name for p, _vf in jobs) == ["a.raw", "b.mkv"]
    assert all(vf == "" for _p, vf in jobs)  # no transform without a summary
    assert any("recording_summary.json" in m for m in handler.messages)


def test_folder_without_summary_transcodes_to_mp4(tmp_path):
    _write_raw(tmp_path / "a.raw", _frame(16, 12))
    _make_mkv(tmp_path / "b.mkv", _frame(16, 12))
    result = _run(str(tmp_path), "--config-dir", str(tmp_path))
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
    assert [p.name for p, _vf in jobs] == ["good.mkv"]
    assert any("0 frames" in m for m in handler.messages)


def test_frameless_folder_transcodes_cleanly_without_error(tmp_path):
    # A folder whose only camera captured 0 frames must not be handed to ffmpeg
    # (which would emit a cryptic matroska error and a non-zero exit); it is
    # skipped, leaving the run successful with nothing transcoded.
    (tmp_path / "cam0.mkv").write_bytes(b"\x00" * 64)
    _summary(tmp_path, [{"file": "cam0.mkv", "frames": 0}])
    result = _run(str(tmp_path), "--config-dir", str(tmp_path))
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
        str(tmp_path / "cam0.raw"), "--as-displayed", "--config-dir", str(tmp_path)
    )
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
        "-r", str(tmp_path), str(sub_b / "cam.raw"), "--config-dir", str(tmp_path)
    )
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
    result = _run(str(tmp_path), "--config-dir", str(tmp_path), "--remove-source")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam0.mp4").exists()
    assert not (tmp_path / "cam0.raw").exists()
    assert not (tmp_path / "cam0.json").exists()  # raw geometry sidecar removed
    assert (tmp_path / "recording_summary.json").exists()  # summary kept


def test_remove_source_deletes_mkv(tmp_path):
    _make_mkv(tmp_path / "cam.mkv", _frame(16, 12))
    result = _run(str(tmp_path), "--config-dir", str(tmp_path), "--remove-source")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cam.mp4").exists()
    assert not (tmp_path / "cam.mkv").exists()


def test_remove_source_keeps_file_when_transcode_fails(tmp_path):
    # A .raw with no .json sidecar fails to transcode; the source must survive.
    (tmp_path / "orphan.raw").write_bytes(_frame(16, 12).tobytes())
    result = _run(str(tmp_path), "--config-dir", str(tmp_path), "--remove-source")
    assert result.exit_code != 0
    assert (tmp_path / "orphan.raw").exists()


def test_config_transcode_defaults_and_format_override(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "octacam_config.toml").write_text(
        '[transcode]\nformat = "mkv"\ncrf = 10\n'
    )
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_raw(rec / "cam.raw", _frame(16, 12))
    # Config default container is mkv.
    assert _run(str(rec), "--config-dir", str(cfg_dir)).exit_code == 0
    assert (rec / "cam.mkv").exists()
    # CLI --format overrides the config default.
    (rec / "cam2.raw").write_bytes((rec / "cam.raw").read_bytes())
    (rec / "cam2.json").write_text((rec / "cam.json").read_text())
    assert (
        _run(
            str(rec / "cam2.raw"), "--config-dir", str(cfg_dir), "--format", "mp4"
        ).exit_code
        == 0
    )
    assert (rec / "cam2.mp4").exists()
