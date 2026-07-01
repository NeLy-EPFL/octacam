"""Writer tests: ffmpeg/raw sinks, transcode roundtrip, failure paths."""

import json
import time

import numpy as np
import pytest

from octacam.config import GuiConfig
from octacam.writer import (
    DEFAULT_CRF,
    FORMATS,
    AsyncFrameWriter,
    FfmpegVideoWriter,
    RawVideoWriter,
    _color_range_args,
    build_x264_args,
    default_codec,
    find_ffmpeg,
    transcode_raw,
)

WIDTH, HEIGHT = 64, 48


def synthetic_frames(n):
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, size=(HEIGHT, WIDTH), dtype=np.uint8)
    frames = []
    for i in range(n):
        frame = base.copy()
        frame[:, : (i * 3) % WIDTH] //= 2  # a moving edge
        frames.append(frame)
    return frames


def read_all_frames(path):
    import cv2

    cap = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames


def test_ffmpeg_writer_lossless_roundtrip(tmp_path):
    frames = synthetic_frames(30)
    out = tmp_path / "test.mkv"
    writer = FfmpegVideoWriter(crf=0)  # crf 0 = lossless: exact roundtrip
    assert writer.open(str(out), 30.0, (WIDTH, HEIGHT))
    for frame in frames:
        assert writer.write(frame)
        time.sleep(0.002)  # pace like a camera; a 0-delay burst would
        # legitimately overflow the bounded queue (drop-on-full)
    writer.close()
    assert not writer.failed, writer.error_tail

    decoded = read_all_frames(out)
    assert len(decoded) == len(frames)
    for src, dec in zip(frames, decoded, strict=True):
        # cv2 decodes to BGR; every channel equals the gray source
        assert np.array_equal(dec[:, :, 0], src)


def test_ffmpeg_writer_remux_mp4(tmp_path):
    out = tmp_path / "test.mkv"
    writer = FfmpegVideoWriter(remux_mp4=True)
    assert writer.open(str(out), 30.0, (WIDTH, HEIGHT))
    for frame in synthetic_frames(10):
        writer.write(frame)
        time.sleep(0.002)
    writer.close()
    assert not out.exists()
    assert len(read_all_frames(tmp_path / "test.mp4")) == 10


def test_ffmpeg_writer_failure_is_reported(tmp_path):
    # Output directory does not exist: ffmpeg exits immediately, the pipe
    # breaks, and the writer must flag failure instead of hanging.
    out = tmp_path / "nonexistent" / "test.mkv"
    writer = FfmpegVideoWriter()
    if writer.open(str(out), 30.0, (WIDTH, HEIGHT)):
        for frame in synthetic_frames(300):
            writer.write(frame)
            if writer.failed:
                break
        writer.close()
        assert writer.failed
    assert not out.exists()


def test_raw_writer_and_transcode_roundtrip(tmp_path):
    frames = synthetic_frames(20)
    out = tmp_path / "cam.raw"
    writer = RawVideoWriter()
    assert writer.open(str(out), 25.0, (WIDTH, HEIGHT))
    for frame in frames:
        assert writer.write(frame)
        time.sleep(0.002)
    writer.close()

    meta = json.loads((tmp_path / "cam.json").read_text())
    assert meta == {
        "width": WIDTH,
        "height": HEIGHT,
        "pixel_format": "Mono8",
        "fps": 25.0,
    }
    assert out.stat().st_size == len(frames) * WIDTH * HEIGHT

    mkv = transcode_raw(out, crf=0, preset="ultrafast")
    decoded = read_all_frames(mkv)
    assert len(decoded) == len(frames)
    assert np.array_equal(decoded[5][:, :, 0], frames[5])


class _SlowSink(AsyncFrameWriter):
    def _open_sink(self, filename, fps, frame_size):
        self.written = []

    def _write_frame(self, frame):
        time.sleep(0.02)
        self.written.append(frame)

    def _close_sink(self):
        pass


def test_drop_on_full_then_drain_on_close(tmp_path):
    writer = _SlowSink(max_queue_size=2)
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    results = [writer.write(frame) for frame in synthetic_frames(10)]
    assert not all(results)  # the slow sink forces drops
    writer.close()
    assert len(writer.written) == sum(results)  # queued frames were drained


def test_format_registry_creates_writers():
    for _name, video_format in FORMATS.items():
        assert video_format.create_writer(2) is not None
        assert video_format.extension
        assert video_format.label


class _FailingSink(AsyncFrameWriter):
    def __init__(self, fail_after, max_queue_size=50):
        super().__init__(max_queue_size)
        self._fail_after = fail_after

    def _open_sink(self, filename, fps, frame_size):
        self.calls = 0

    def _write_frame(self, frame):
        self.calls += 1
        if self.calls > self._fail_after:
            raise OSError("sink died")

    def _close_sink(self):
        pass


def test_frames_written_reflects_actual_writes_on_failure():
    # When the sink dies, frames_written must count only what actually
    # reached it, so the grab loop can mark the discarded tail as dropped.
    writer = _FailingSink(fail_after=3)
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    for frame in synthetic_frames(10):
        writer.write(frame)
        time.sleep(0.005)
    writer.close()
    assert writer.failed
    assert writer.frames_written == 3


def test_build_x264_args_threads_x264_params():
    common = dict(ffmpeg="ffmpeg", output="o.mkv", fps=30.0, width=64, height=48)

    # Omitted when blank: no stray -x264-params flag.
    bare = build_x264_args(crf=18, preset="ultrafast", pix_fmt="gray", **common)
    assert "-x264-params" not in bare
    assert bare[bare.index("-crf") + 1] == "18"
    assert bare[bare.index("-preset") + 1] == "ultrafast"

    # Passed verbatim as a single token when set.
    extra = build_x264_args(
        crf=18,
        preset="ultrafast",
        pix_fmt="gray",
        x264_params="keyint=30:scenecut=0",
        **common,
    )
    assert extra[extra.index("-x264-params") + 1] == "keyint=30:scenecut=0"


def test_color_range_args_only_for_limited_range_yuv():
    # YUV pixel formats default to limited/TV range (luma squeezed into 16-235);
    # we force full range so 0-255 frames survive. gray (4:0:0) and the yuvj*
    # aliases are already full range and need no flag.
    assert _color_range_args("yuv420p") == ["-color_range", "pc"]
    assert _color_range_args("yuv444p") == ["-color_range", "pc"]
    assert _color_range_args("gray") == []
    assert _color_range_args("yuvj420p") == []


def test_build_x264_args_forces_full_range_for_yuv_only():
    common = dict(
        ffmpeg="ffmpeg", output="o.mp4", fps=30.0, width=64, height=48,
        crf=0, preset="ultrafast",
    )
    yuv = build_x264_args(pix_fmt="yuv420p", **common)
    assert yuv[yuv.index("-color_range") + 1] == "pc"
    # gray is already full range: no stray -color_range flag.
    assert "-color_range" not in build_x264_args(pix_fmt="gray", **common)


def test_yuv420p_transcode_preserves_full_range(tmp_path):
    # Regression: a 0-255 ramp transcoded to yuv420p (lossless) must come back
    # spanning the full range, NOT clamped into limited range's 16-235. Decodes
    # straight to full-range gray and inspects the actual luma span.
    import subprocess

    w, h = 256, 16
    ramp = np.tile(np.arange(256, dtype=np.uint8), (h, 1))
    raw = tmp_path / "ramp.raw"
    raw.write_bytes(ramp.tobytes())
    raw.with_suffix(".json").write_text(
        json.dumps({"width": w, "height": h, "pixel_format": "Mono8", "fps": 10.0})
    )

    out = transcode_raw(
        raw, crf=0, preset="ultrafast", output=tmp_path / "ramp.mp4", pix_fmt="yuv420p"
    )

    dec = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(out),
         "-f", "rawvideo", "-pixel_format", "gray", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    back = np.frombuffer(dec, dtype=np.uint8)[: w * h].reshape(h, w)
    # The old limited-range default clamped to [16, 235]; full range reaches the
    # extremes (lossless crf 0, so this is effectively exact).
    assert back.min() <= 2 and back.max() >= 253, (int(back.min()), int(back.max()))


def test_capture_default_crf_is_18():
    # The capture default lives in one place (writer.DEFAULT_CRF) and is shared
    # by the writer and the x264 format entry.
    assert DEFAULT_CRF == 18
    assert FfmpegVideoWriter().crf == 18
    assert FORMATS["x264"].crf == 18


def test_default_codec_resolution():
    assert default_codec(GuiConfig()) == "x264"  # index 0
    assert default_codec(GuiConfig(video_writer_default_index=1)) == "raw"
    # an out-of-range index falls back to x264
    assert default_codec(GuiConfig(video_writer_default_index=9)) == "x264"
    # the named key overrides the index, and unknown names fall back to x264
    assert default_codec(GuiConfig(video_writer_default="raw")) == "raw"
    assert (
        default_codec(
            GuiConfig(video_writer_default="raw", video_writer_default_index=0)
        )
        == "raw"
    )
    assert default_codec(GuiConfig(video_writer_default="bogus")) == "x264"


def test_transcode_missing_sidecar_raises(tmp_path):
    raw = tmp_path / "orphan.raw"
    raw.write_bytes(b"\x00" * (WIDTH * HEIGHT))
    with pytest.raises(FileNotFoundError):
        transcode_raw(raw)
