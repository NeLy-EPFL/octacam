"""NVENC (GPU) encode path: detection, session-cap fallback, and real-GPU encodes.

The unit tests mock the capability probe so they run anywhere (CI has no GPU);
the ``@requires_nvenc`` integration tests skip unless a working ``h264_nvenc``
ffmpeg is present on this machine.
"""

import json
import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import numpy as np
import pytest

from octacam import writer as w
from octacam.config import RecordConfig
from octacam.controller import RecordingController, RecordingSettings
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    FORMATS,
    NVENC_H264_PARAMS,
    cpu_fallback_format,
    encoder_of,
    is_nvenc_params,
    resolve_capture_formats,
)

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]


def _nvenc_available() -> bool:
    try:
        w.find_ffmpeg(require_encoder="h264_nvenc")
        return True
    except RuntimeError:
        return False


requires_nvenc = pytest.mark.skipif(
    not _nvenc_available(), reason="no working h264_nvenc ffmpeg on this machine"
)


# --- encoder parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "params, encoder, nvenc",
    [
        (NVENC_H264_PARAMS, "h264_nvenc", True),
        ("-c:v hevc_nvenc -cq 20", "hevc_nvenc", True),
        ("-vcodec av1_nvenc", "av1_nvenc", True),
        (DEFAULT_FFMPEG_PARAMS, "libx264", False),
        ("-preset fast -pix_fmt gray", None, False),  # no -c:v
        ("-c:v 'unterminated", None, False),  # bad quoting -> None, not a crash
    ],
)
def test_encoder_of_and_is_nvenc(params, encoder, nvenc):
    assert encoder_of(params) == encoder
    assert is_nvenc_params(params) is nvenc


def test_required_encoder_only_for_gpu():
    # CPU work runs on any ffmpeg (no capability search); only *_nvenc needs one.
    assert w._required_encoder(DEFAULT_FFMPEG_PARAMS) is None
    assert w._required_encoder(NVENC_H264_PARAMS) == "h264_nvenc"
    assert w._required_encoder("-c:v hevc_nvenc") == "hevc_nvenc"


# --- find_ffmpeg(require_encoder) probe + cache -----------------------------


def test_find_ffmpeg_default_needs_no_probe(monkeypatch):
    # The historical no-arg path must never run a capability probe.
    monkeypatch.setattr(
        w, "ffmpeg_encoder_works", lambda *a, **k: pytest.fail("probed")
    )
    assert w.find_ffmpeg()  # bundled/PATH ffmpeg, no probe


def test_find_ffmpeg_require_encoder_picks_first_working(monkeypatch):
    monkeypatch.setattr(w, "_FFMPEG_FOR_ENCODER", {})
    monkeypatch.setattr(w, "_ENCODER_OK", {})
    monkeypatch.setattr(
        w, "_ffmpeg_candidates", lambda: ["/no/nvenc", "/has/nvenc", "/also"]
    )
    monkeypatch.setattr(w, "ffmpeg_encoder_works", lambda exe, enc: exe == "/has/nvenc")
    assert w.find_ffmpeg(require_encoder="h264_nvenc") == "/has/nvenc"
    # Cached: a second call must not recompute the candidate list.
    monkeypatch.setattr(
        w, "_ffmpeg_candidates", lambda: pytest.fail("recomputed after cache")
    )
    assert w.find_ffmpeg(require_encoder="h264_nvenc") == "/has/nvenc"


def test_find_ffmpeg_require_encoder_raises_when_none(monkeypatch):
    monkeypatch.setattr(w, "_FFMPEG_FOR_ENCODER", {})
    monkeypatch.setattr(w, "_ENCODER_OK", {})
    monkeypatch.setattr(w, "_ffmpeg_candidates", lambda: ["/a", "/b"])
    monkeypatch.setattr(w, "ffmpeg_encoder_works", lambda exe, enc: False)
    with pytest.raises(RuntimeError, match="h264_nvenc"):
        w.find_ffmpeg(require_encoder="h264_nvenc")


# --- resolve_capture_formats ------------------------------------------------


def test_resolve_non_nvenc_passthrough():
    base = FORMATS["ffmpeg"]
    formats, warns = resolve_capture_formats(base, 4, 8)
    assert formats == [base] * 4
    assert warns == []


def test_resolve_zero_cameras():
    assert resolve_capture_formats(FORMATS["nvenc"], 0, 8) == ([], [])


def test_resolve_all_gpu_under_cap(monkeypatch):
    monkeypatch.setattr(w, "find_ffmpeg", lambda require_encoder=None: "/ok")
    formats, warns = resolve_capture_formats(FORMATS["nvenc"], 3, 8)
    assert all(is_nvenc_params(f.ffmpeg_params) for f in formats)
    assert warns == []


def test_resolve_overflow_to_cpu(monkeypatch):
    monkeypatch.setattr(w, "find_ffmpeg", lambda require_encoder=None: "/ok")
    formats, warns = resolve_capture_formats(FORMATS["nvenc"], 10, 8)
    assert sum(is_nvenc_params(f.ffmpeg_params) for f in formats) == 8
    assert sum("libx264" in f.ffmpeg_params for f in formats) == 2
    # NVENC cameras come first; the CPU overflow is the tail.
    assert is_nvenc_params(formats[7].ffmpeg_params)
    assert "libx264" in formats[8].ffmpeg_params
    assert len(warns) == 1 and "exceed the NVENC session limit" in warns[0]


def test_resolve_unavailable_falls_back_all_cpu(monkeypatch):
    def _boom(require_encoder=None):
        raise RuntimeError("no nvenc here")

    monkeypatch.setattr(w, "find_ffmpeg", _boom)
    formats, warns = resolve_capture_formats(FORMATS["nvenc"], 3, 8)
    assert all("libx264" in f.ffmpeg_params for f in formats)
    assert len(warns) == 1 and "unavailable" in warns[0]


def test_resolve_cap_zero_forces_cpu(monkeypatch):
    monkeypatch.setattr(w, "find_ffmpeg", lambda require_encoder=None: "/ok")
    formats, warns = resolve_capture_formats(FORMATS["nvenc"], 3, 0)
    assert all("libx264" in f.ffmpeg_params for f in formats)
    assert len(warns) == 1


def test_cpu_fallback_mirrors_container():
    base = w.VideoFormat(
        "ffmpeg", "mp4", "x", ffmpeg_params=NVENC_H264_PARAMS, remux_mp4=True
    )
    cpu = cpu_fallback_format(base)
    assert cpu.extension == "mp4" and cpu.remux_mp4 is True
    assert "libx264" in cpu.ffmpeg_params and not is_nvenc_params(cpu.ffmpeg_params)
    # Preserves the base pixel format (yuv420p), so a mixed GPU+CPU recording is
    # uniform (all yuv420p) rather than the fallback emitting monochrome 4:0:0.
    assert "-pix_fmt yuv420p" in cpu.ffmpeg_params


def test_cpu_fallback_defaults_pix_fmt_when_base_has_none():
    base = w.VideoFormat("ffmpeg", "mkv", "x", ffmpeg_params="-c:v h264_nvenc -cq 20")
    assert "-pix_fmt gray" in cpu_fallback_format(base).ffmpeg_params


# --- RecordingSettings.video_format -----------------------------------------


def test_video_format_nvenc_uses_curated_params_by_default():
    # ffmpeg_params left at the libx264 default => the GPU choice wins.
    fmt = RecordingSettings(save_method="nvenc").video_format()
    assert fmt.ffmpeg_params == NVENC_H264_PARAMS
    assert fmt.save_method == "ffmpeg"  # still an FfmpegVideoWriter under the hood


def test_video_format_nvenc_honours_custom_nvenc_params():
    custom = "-c:v hevc_nvenc -cq 20 -pix_fmt yuv420p"
    fmt = RecordingSettings(save_method="nvenc", ffmpeg_params=custom).video_format()
    assert fmt.ffmpeg_params == custom


def test_video_format_ffmpeg_unchanged():
    custom = "-c:v libx264 -crf 20 -pix_fmt gray"
    fmt = RecordingSettings(save_method="ffmpeg", ffmpeg_params=custom).video_format()
    assert fmt.ffmpeg_params == custom


# --- config -----------------------------------------------------------------


def test_config_accepts_nvenc_and_sessions():
    r = RecordConfig.model_validate({"save_method": "nvenc", "max_nvenc_sessions": 6})
    assert r.save_method == "nvenc" and r.max_nvenc_sessions == 6


def test_config_floors_negative_sessions():
    assert RecordConfig.model_validate({"max_nvenc_sessions": -5}).max_nvenc_sessions == 0


# --- CameraSystem.start_record per-camera format list -----------------------


def test_start_record_rejects_wrong_length_format_list():
    from octacam.cameras import CameraSystem

    system = CameraSystem(FAKE_SERIALS, backend="fake")
    try:
        with pytest.raises(ValueError, match="formats for"):
            system.start_record("/tmp/x", 30.0, [FORMATS["ffmpeg"]])  # 1 fmt, 2 cams
    finally:
        system.close()


# --- real GPU (skipped without working NVENC) -------------------------------


@requires_nvenc
def test_nvenc_writer_encodes_real_gpu(tmp_path):
    from octacam.writer import FfmpegVideoWriter

    out = tmp_path / "nv.mkv"
    writer = FfmpegVideoWriter(ffmpeg_params=NVENC_H264_PARAMS)
    assert writer.open(str(out), 30.0, (640, 480))  # (width, height)
    rng = np.random.default_rng(0)
    for _ in range(30):
        writer.write(rng.integers(0, 255, size=(480, 640), dtype=np.uint8))
    writer.close()
    assert not writer.failed, writer.error_tail
    assert out.stat().st_size > 0

    import cv2

    cap = cv2.VideoCapture(str(out))
    n = 0
    while cap.read()[0]:
        n += 1
    cap.release()
    assert n >= 25  # ~30 frames survive the encode/decode round-trip


def _fake_nvenc_system(tmp_path):
    from octacam.cameras import CameraSystem

    system = CameraSystem(FAKE_SERIALS, backend="fake")
    system.load_config(tmp_path)
    for camera in system:
        camera.set_geometry(width=640, height=480)  # >= NVENC minimum dims
    return system


@requires_nvenc
def test_fake_recording_with_nvenc(tmp_path):
    system = _fake_nvenc_system(tmp_path)
    save_dir = tmp_path / "rec"
    settings = RecordingSettings(
        fps=30.0, duration_s=1.0, save_dir=str(save_dir), save_method="nvenc"
    )
    controller = RecordingController(system, settings, auto_preview=False)
    try:
        assert controller.start_recording().ok
        controller.join(timeout=30)
        summary = json.loads((save_dir / "recording_summary.json").read_text())
        assert summary["save_method"] == "nvenc"
        # The summary records the encoder actually used (resolved NVENC params),
        # not the raw libx264 default that was left in ffmpeg_params.
        assert "h264_nvenc" in summary["ffmpeg_params"]
        assert summary["max_nvenc_sessions"] == 8
        assert all(c["frames"] > 0 for c in summary["cameras"])
        videos = sorted(save_dir.glob("*.mkv"))
        assert len(videos) == 2 and all(v.stat().st_size > 0 for v in videos)
    finally:
        system.close()


@requires_nvenc
def test_fake_recording_nvenc_overflow_splits(tmp_path):
    # 2 cameras, cap = 1: the first camera encodes on GPU, the second falls back
    # to CPU (libx264). Both must still record, and the operator is warned. This
    # exercises the more-cameras-than-sessions split cheaply (only 1 GPU session).
    system = _fake_nvenc_system(tmp_path)
    save_dir = tmp_path / "rec"
    settings = RecordingSettings(
        fps=30.0,
        duration_s=1.0,
        save_dir=str(save_dir),
        save_method="nvenc",
        max_nvenc_sessions=1,
    )
    controller = RecordingController(system, settings, auto_preview=False)
    try:
        assert controller.start_recording().ok
        controller.join(timeout=30)
        assert any(
            e["level"] == "warning" and "session limit" in e["message"]
            for e in controller.events
        )
        summary = json.loads((save_dir / "recording_summary.json").read_text())
        assert all(c["frames"] > 0 for c in summary["cameras"])  # GPU + CPU both wrote
    finally:
        system.close()
