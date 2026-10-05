"""Capture writers: ffmpeg/raw sinks, fills, failure paths."""

import subprocess
import time

import numpy as np
import pytest
from helpers import wait_until

from octacam.transcode import transcode_file
from octacam.writer import (
    DEFAULT_CRF,
    FORMATS,
    AsyncFrameWriter,
    FfmpegVideoWriter,
    RawVideoWriter,
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
    # crf 0 = lossless: exact roundtrip
    writer = FfmpegVideoWriter(
        ffmpeg_params="-c:v libx264 -preset ultrafast -crf 0 -pix_fmt gray"
    )
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


@pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt])
def test_ffmpeg_open_reaps_child_when_stderr_thread_fails(tmp_path, monkeypatch, exc):
    # A thread that fails to start after Popen (e.g. thread exhaustion, or a
    # Ctrl-C landing there) must not orphan the ffmpeg child: close() skips a
    # writer whose thread never ran.
    import octacam.writer as writer_mod

    created = []
    real_popen = writer_mod.subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(writer_mod.subprocess, "Popen", recording_popen)

    class BoomThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise exc("can't start new thread")

    monkeypatch.setattr(writer_mod.threading, "Thread", BoomThread)

    out = tmp_path / "test.mkv"
    writer = FfmpegVideoWriter(
        ffmpeg_params="-c:v libx264 -preset ultrafast -crf 0 -pix_fmt gray"
    )
    if issubclass(exc, Exception):
        assert writer.open(str(out), 30.0, (WIDTH, HEIGHT)) is False
    else:
        with pytest.raises(exc):
            writer.open(str(out), 30.0, (WIDTH, HEIGHT))
    assert created, "expected an ffmpeg child to have been spawned"
    assert created[0].poll() is not None  # reaped: killed + waited, not orphaned
    assert writer._proc is None
    writer.close()  # no-op (writer thread never started), must not raise


def test_open_releases_the_sink_when_the_writer_thread_fails(tmp_path, monkeypatch):
    import octacam.writer as writer_mod

    class BoomThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(writer_mod.threading, "Thread", BoomThread)
    writer = RawVideoWriter()
    assert writer.open(str(tmp_path / "cam.raw"), 30.0, (WIDTH, HEIGHT)) is False
    assert writer._file is None
    assert not writer.write(np.zeros((HEIGHT, WIDTH), np.uint8))
    writer.close()


def test_raw_writer_and_transcode_roundtrip(tmp_path):
    frames = synthetic_frames(20)
    out = tmp_path / "cam.raw"
    writer = RawVideoWriter()
    assert writer.open(str(out), 25.0, (WIDTH, HEIGHT))
    for frame in frames:
        assert writer.write(frame)
        time.sleep(0.002)
    writer.close()

    # RawVideoWriter only writes the .raw stream; geometry lives in the
    # recording summary, so no per-camera sidecar is produced any more.
    assert not (tmp_path / "cam.json").exists()
    assert out.stat().st_size == len(frames) * WIDTH * HEIGHT

    # Geometry must be supplied explicitly to transcode a raw dump.
    mkv = transcode_file(
        out,
        tmp_path / "cam.mkv",
        ffmpeg_params="-c:v libx264 -preset ultrafast -crf 0 -pix_fmt gray",
        width=WIDTH,
        height=HEIGHT,
        fps=25.0,
    )
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


def test_capture_pipes_rawvideo_into_the_configured_encoder(tmp_path, monkeypatch):
    # The input geometry is derived, the params are spliced verbatim, and frames
    # go unbuffered down the stdin pipe.
    import octacam.writer as writer_mod

    launched = []
    real_popen = writer_mod.subprocess.Popen

    def recording_popen(args, **kwargs):
        launched.append((args, kwargs))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(writer_mod.subprocess, "Popen", recording_popen)
    out = tmp_path / "cam.mkv"
    writer = FfmpegVideoWriter(
        ffmpeg_params="-c:v libx264 -preset ultrafast -crf 18 -pix_fmt gray"
    )
    assert writer.open(str(out), 30.0, (WIDTH, HEIGHT))
    for frame in synthetic_frames(3):
        assert writer.write(frame)
    writer.close()
    assert not writer.failed, writer.error_tail
    ((args, kwargs),) = launched
    assert args[args.index("-video_size") + 1] == f"{WIDTH}x{HEIGHT}"
    assert args[args.index("-framerate") + 1] == "30"
    assert args[args.index("-pixel_format") + 1] == "gray"  # input pix fmt
    assert args[args.index("-i") + 1] == "pipe:0"
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "ultrafast"
    assert args[-1] == str(out)
    assert kwargs["stdin"] is subprocess.PIPE and kwargs["bufsize"] == 0


def test_capture_default_crf_is_18():
    # The capture default lives in one place (writer.DEFAULT_CRF) and is folded
    # into the default ffmpeg_params of both the writer and the x264 format entry.
    assert DEFAULT_CRF == 18
    assert "-crf 18" in FfmpegVideoWriter().ffmpeg_params
    assert "-crf 18" in FORMATS["ffmpeg"].ffmpeg_params


class _ListSink(AsyncFrameWriter):
    def _open_sink(self, filename, fps, frame_size):
        self.written = []

    def _write_frame(self, frame):
        self.written.append(frame)

    def _close_sink(self):
        pass


def test_fill_before_repeats_the_previous_frame():
    # A missed trigger pulse rides on the next frame: the writer repeats the
    # frame before it, so the file keeps one frame per pulse.
    writer = _ListSink(max_queue_size=10)
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    a, b, c = synthetic_frames(3)
    assert writer.write(a)
    assert writer.write(b, fill_before=2)
    assert writer.write(c)
    writer.close(fill_after=1)
    assert [id(f) for f in writer.written] == [id(a), id(a), id(a), id(b), id(c), id(c)]
    assert writer.frames_written == 6


def test_a_leading_fill_repeats_the_first_frame():
    # Nothing written yet (the camera missed the train's first pulse): the fill
    # repeats the frame it precedes rather than inventing one.
    writer = _ListSink(max_queue_size=10)
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    (a,) = synthetic_frames(1)
    assert writer.write(a, fill_before=2)
    writer.close()
    assert [id(f) for f in writer.written] == [id(a)] * 3


def test_a_fill_is_never_dropped_on_its_own():
    # The fill travels with its frame: if the queue is full both are refused
    # together (the caller re-owes them on the next write), never the fill alone.
    writer = _SlowSink(max_queue_size=1)
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    frames = synthetic_frames(6)
    owed, accepted = 0, 0
    for frame in frames:
        if writer.write(frame, fill_before=owed):
            accepted += 1 + owed
            owed = 0
        else:
            owed += 1
    writer.close(fill_after=owed)
    assert len(writer.written) == len(frames)


class _GatedSink(AsyncFrameWriter):
    """Writes a frame only once the test lets it through."""

    def _open_sink(self, filename, fps, frame_size):
        import threading

        self.gate = threading.Semaphore(0)
        self.written = []

    def _write_frame(self, frame):
        self.gate.acquire()
        self.written.append(frame)

    def _close_sink(self):
        pass


def test_backlog_counts_the_fills_riding_on_each_queued_frame():
    # The grab loop tells a transient stall from a sustained shortfall by how
    # far behind the encoder is, and the queue's item count hides the fills.
    writer = _GatedSink(max_queue_size=4)
    assert writer.max_queue_size == 4
    assert writer.open("ignored", 30.0, (WIDTH, HEIGHT))
    a, b = synthetic_frames(2)
    assert writer.backlog == 0
    assert writer.write(a)
    assert writer.write(b, fill_before=3)
    assert writer.backlog == 5
    for _ in range(3):
        writer.gate.release()
    wait_until(lambda: writer.frames_written >= 3, timeout=2.0, interval=0.001)
    assert writer.backlog == 2
    for _ in range(2):
        writer.gate.release()
    writer.close()
    assert writer.backlog == 0 and writer.frames_written == 5
