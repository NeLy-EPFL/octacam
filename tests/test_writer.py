"""Writer tests: ffmpeg/raw sinks, transcode roundtrip, failure paths."""

import json
import time

import numpy as np
import pytest

from octacam.config import GuiConfig
from octacam.writer import (
    FORMATS,
    AsyncFrameWriter,
    FfmpegVideoWriter,
    RawVideoWriter,
    default_codec,
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
    for src, dec in zip(frames, decoded):
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
    for name, video_format in FORMATS.items():
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


def test_default_codec_resolution():
    assert default_codec(GuiConfig()) == "x264"  # index 0
    assert default_codec(GuiConfig(video_writer_default_index=2)) == "mjpg"
    # the named key overrides the index, and unknown names fall back to x264
    assert default_codec(GuiConfig(video_writer_default="h264")) == "h264"
    assert (
        default_codec(GuiConfig(video_writer_default="h264",
                                video_writer_default_index=0)) == "h264"
    )
    assert default_codec(GuiConfig(video_writer_default="bogus")) == "x264"


def test_transcode_missing_sidecar_raises(tmp_path):
    raw = tmp_path / "orphan.raw"
    raw.write_bytes(b"\x00" * (WIDTH * HEIGHT))
    with pytest.raises(FileNotFoundError):
        transcode_raw(raw)
