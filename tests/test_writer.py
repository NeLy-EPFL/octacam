"""Writer tests: ffmpeg/raw sinks, transcode roundtrip, failure paths."""

import functools
import time

import numpy as np
import pytest
from helpers import wait_until

from octacam.writer import (
    DEFAULT_CRF,
    FORMATS,
    AsyncFrameWriter,
    FfmpegVideoWriter,
    RawVideoWriter,
    _color_range_args,
    _split_opts,
    build_encode_args,
    find_ffmpeg,
    transcode_encoded,
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
    mkv = transcode_raw(
        out,
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


def test_build_encode_args_derives_input_and_splices_params_verbatim():
    args = build_encode_args(
        "ffmpeg",
        "o.mkv",
        30.0,
        64,
        48,
        "-c:v libx264 -preset ultrafast -crf 18 -pix_fmt gray",
    )
    # Derived input args from the frame geometry.
    assert args[args.index("-video_size") + 1] == "64x48"
    assert args[args.index("-framerate") + 1] == "30"
    assert args[args.index("-pixel_format") + 1] == "gray"  # input pix fmt
    assert args[args.index("-i") + 1] == "pipe:0"
    # Encoder args spliced verbatim from ffmpeg_params.
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "ultrafast"
    assert args[-1] == "o.mkv"


def test_build_encode_args_uses_source_and_input_pix_fmt():
    args = build_encode_args(
        "ffmpeg",
        "o.mkv",
        10.0,
        8,
        6,
        "-c:v libx264 -pix_fmt gray",
        source="in.raw",
        input_pix_fmt="gray",
    )
    assert args[args.index("-i") + 1] == "in.raw"


def test_split_opts_pulls_options_and_their_values():
    vf = ("-vf", "-filter:v")
    tokens = ["-c:v", "libx264", "-vf", "eq=contrast=2", "-crf", "18"]
    rest = ["-c:v", "libx264", "-crf", "18"]
    assert _split_opts(tokens, vf) == (rest, ["eq=contrast=2"])
    both = ["-filter:v", "hflip", "-vf", "vflip"]
    assert _split_opts(both, vf) == ([], ["hflip", "vflip"])
    assert _split_opts(["-c:v", "libx264"], vf) == (["-c:v", "libx264"], [])
    # A trailing option with no value is dropped; flags go without a value.
    assert _split_opts(["-y", "-vf"], vf) == (["-y"], [])
    flagged = ["-stats", "-i", "x"]
    assert _split_opts(flagged, (), flags=("-stats",)) == (["-i", "x"], [])


def test_build_encode_args_merges_user_vf():
    args = build_encode_args(
        "ffmpeg",
        "o.mkv",
        30.0,
        64,
        48,
        "-c:v libx264 -vf eq=contrast=2 -pix_fmt gray",
    )
    # ffmpeg takes one -vf: the user's filter, then the full-range conversion of
    # the yuv420p the gray output is written as.
    assert args.count("-vf") == 1
    assert args[args.index("-vf") + 1] == "eq=contrast=2,scale=out_range=full"


def test_color_range_args_only_for_limited_range_yuv():
    # YUV pixel formats default to limited/TV range (luma squeezed into 16-235);
    # we force full range so 0-255 frames survive. gray (4:0:0) and the yuvj*
    # aliases are already full range and need no flag.
    assert _color_range_args("yuv420p") == ["-color_range", "pc"]
    assert _color_range_args("yuv444p") == ["-color_range", "pc"]
    assert _color_range_args("gray") == []
    assert _color_range_args("yuvj420p") == []


def test_build_encode_args_forces_full_range_for_yuv_only():
    common = dict(ffmpeg="ffmpeg", output="o.mp4", fps=30.0, width=64, height=48)
    yuv = build_encode_args(
        ffmpeg_params="-c:v libx264 -crf 0 -preset ultrafast -pix_fmt yuv420p",
        **common,
    )
    assert yuv[yuv.index("-color_range") + 1] == "pc"
    # The full-range conversion filter is injected into the merged -vf.
    assert "scale=out_range=full" in yuv[yuv.index("-vf") + 1]

    # gray is already full range: no stray -color_range flag, no injected filter.
    # (An odd frame keeps libx264's gray; an even one is written as yuv420p.)
    gray = build_encode_args(
        ffmpeg_params="-c:v libx264 -crf 0 -preset ultrafast -pix_fmt gray",
        **{**common, "width": 63},
    )
    assert gray[gray.index("-pix_fmt") + 1] == "gray"
    assert "-color_range" not in gray
    assert "-vf" not in gray


def _out_pix_fmt(args: list[str]) -> str:
    return args[args.index("-pix_fmt") + 1]


@pytest.mark.parametrize("encoder", ["libx264", "libx265"])
def test_gray_h264_is_written_as_full_range_yuv420p(encoder):
    # Monochrome 4:0:0 H.264 decodes as flat gray frames on NVIDIA hardware
    # decoders (VLC's default there), so a gray output — every older config and
    # recording snapshot — is written as full-range 4:2:0 instead.
    args = build_encode_args(
        "ffmpeg", "o.mkv", 30.0, 64, 48, f"-c:v {encoder} -crf 18 -pix_fmt gray"
    )
    assert _out_pix_fmt(args) == "yuv420p"
    assert args[args.index("-color_range") + 1] == "pc"
    assert "scale=out_range=full" in args[args.index("-vf") + 1]


@pytest.mark.parametrize("size", [(63, 48), (64, 47)])
def test_odd_frames_stay_monochrome(size):
    # 4:2:0 cannot code an odd side (libx264: "width not divisible by 2"), so
    # gray is kept — and a yuv420p default falls back to it — rather than failing.
    for pix_fmt in ("gray", "yuv420p"):
        args = build_encode_args(
            "ffmpeg", "o.mkv", 30.0, *size, f"-c:v libx264 -pix_fmt {pix_fmt}"
        )
        assert _out_pix_fmt(args) == "gray"
        assert "-color_range" not in args


def test_gray_is_left_alone_for_other_encoders_and_unknown_sizes(tmp_path, monkeypatch):
    # FFV1 and friends decode gray in software everywhere: nothing to fix.
    args = build_encode_args("ffmpeg", "o.mkv", 30.0, 64, 48, "-c:v ffv1 -pix_fmt gray")
    assert _out_pix_fmt(args) == "gray"
    # A re-encode with no known frame size cannot rule out an odd side.
    captured = {}
    monkeypatch.setattr(
        "octacam.writer._run_ffmpeg", lambda args, *a, **k: captured.update(args=args)
    )
    transcode_encoded(
        tmp_path / "in.mkv", tmp_path / "out.mp4", "-c:v libx264 -pix_fmt gray"
    )
    assert _out_pix_fmt(captured["args"]) == "gray"
    transcode_encoded(
        tmp_path / "in.mkv",
        tmp_path / "out.mp4",
        "-c:v libx264 -pix_fmt gray",
        width=64,
        height=48,
    )
    assert _out_pix_fmt(captured["args"]) == "yuv420p"


def test_gray_config_encodes_4_2_0_with_exact_luma(tmp_path):
    # End to end through a real ffmpeg: a lossless "-pix_fmt gray" encode is a
    # 4:2:0 stream (chroma_format_idc 1, which hardware decoders support) whose
    # luma decodes bit-exact.
    import subprocess

    w, h = 256, 16
    ramp = np.tile(np.arange(256, dtype=np.uint8), (h, 1))
    raw = tmp_path / "ramp.raw"
    raw.write_bytes(ramp.tobytes())
    out = transcode_raw(
        raw,
        ffmpeg_params="-c:v libx264 -qp 0 -preset ultrafast -pix_fmt gray",
        output=tmp_path / "ramp.mkv",
        width=w,
        height=h,
        fps=10.0,
    )
    ffmpeg = find_ffmpeg()
    headers = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(out),
            "-c",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    (line,) = [ln for ln in headers.splitlines() if "chroma_format_idc" in ln][:1]
    assert line.rstrip().endswith("= 1"), line
    dec = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    back = np.frombuffer(dec, dtype=np.uint8)[: w * h].reshape(h, w)
    assert np.array_equal(back, ramp)


def test_yuv420p_transcode_preserves_full_range(tmp_path):
    # Regression: a 0-255 ramp transcoded to yuv420p (lossless) must come back
    # spanning the full range, NOT clamped into limited range's 16-235. Decodes
    # straight to full-range gray and inspects the actual luma span.
    import subprocess

    w, h = 256, 16
    ramp = np.tile(np.arange(256, dtype=np.uint8), (h, 1))
    raw = tmp_path / "ramp.raw"
    raw.write_bytes(ramp.tobytes())

    out = transcode_raw(
        raw,
        ffmpeg_params="-c:v libx264 -crf 0 -preset veryslow -pix_fmt yuv420p",
        output=tmp_path / "ramp.mp4",
        width=w,
        height=h,
        fps=10.0,
    )

    dec = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    back = np.frombuffer(dec, dtype=np.uint8)[: w * h].reshape(h, w)
    # The old limited-range default clamped to [16, 235]; full range reaches the
    # extremes (lossless crf 0, so this is effectively exact).
    assert back.min() <= 2 and back.max() >= 253, (int(back.min()), int(back.max()))


def test_capture_default_crf_is_18():
    # The capture default lives in one place (writer.DEFAULT_CRF) and is folded
    # into the default ffmpeg_params of both the writer and the x264 format entry.
    assert DEFAULT_CRF == 18
    assert "-crf 18" in FfmpegVideoWriter().ffmpeg_params
    assert "-crf 18" in FORMATS["ffmpeg"].ffmpeg_params


def test_transcode_raw_without_geometry_raises(tmp_path):
    # A .raw stream carries no geometry: without width/height/fps the frame
    # layout is unknown and transcoding must raise (there is no sidecar to read).
    raw = tmp_path / "orphan.raw"
    raw.write_bytes(b"\x00" * (WIDTH * HEIGHT))
    with pytest.raises(FileNotFoundError):
        transcode_raw(raw)
    # Supplying only a partial geometry is still insufficient.
    with pytest.raises(FileNotFoundError):
        transcode_raw(raw, width=WIDTH, height=HEIGHT)


def test_ffmpeg_probes_and_transcode_never_grab_the_tty(tmp_path, monkeypatch):
    # Regression: an ffmpeg with the controlling terminal on stdin flips the tty
    # to no-echo/cbreak (to watch for keypresses) and only restores on a clean
    # exit. `octacam doctor`'s NVENC probes launch encodes (probe_nvenc_max_sessions
    # runs 12 concurrent ones — their save/restore reliably races echo off), and a
    # timeout-killed encode never restores at all, leaving the user's shell with
    # invisible input. Every probe/transcode launch must keep ffmpeg off the tty:
    # -nostdin in the args AND stdin=subprocess.DEVNULL. (The record path is
    # exempt — it feeds frames through a stdin PIPE — and is not covered here.)
    import subprocess
    from types import SimpleNamespace

    import octacam.writer as writer_mod

    def assert_off_tty(cmd, kwargs, what):
        assert "-nostdin" in cmd, f"{what}: missing -nostdin in {cmd}"
        assert kwargs.get("stdin") is subprocess.DEVNULL, (
            f"{what}: stdin not redirected to DEVNULL (kwargs={kwargs})"
        )

    # 1. ffmpeg_encoder_works — a single real encode via subprocess.run, run
    #    through a fresh cache so the process's real probe results survive.
    probe = functools.cache(writer_mod.ffmpeg_encoder_works.__wrapped__)
    monkeypatch.setattr(writer_mod, "ffmpeg_encoder_works", probe)
    runs: list[tuple[list, dict]] = []

    def fake_run(cmd, **kwargs):
        runs.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(writer_mod.subprocess, "run", fake_run)
    assert probe("/fake/ffmpeg", "h264_nvenc") is True
    assert runs, "encoder probe should have launched ffmpeg"
    assert_off_tty(runs[0][0], runs[0][1], "ffmpeg_encoder_works")

    # 2. probe_nvenc_max_sessions — up to 12 concurrent encodes via Popen.
    monkeypatch.setattr(writer_mod, "find_ffmpeg", lambda **_: "/fake/ffmpeg")
    launches: list[tuple[list, dict]] = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            launches.append((cmd, kwargs))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(writer_mod.subprocess, "Popen", FakeProc)
    assert writer_mod.probe_nvenc_max_sessions(ceiling=12) == 12
    assert len(launches) == 12
    for cmd, kwargs in launches:
        assert_off_tty(cmd, kwargs, "probe_nvenc_max_sessions")

    # 3. _reporting_args injects -nostdin for both the piped-progress and the
    #    opt-in raw-view transcode modes (grid xstack routes through here too).
    for raw in (False, True):
        flags = writer_mod._reporting_args(["/fake/ffmpeg", "-i", "in.mkv"], raw)
        assert "-nostdin" in flags, f"_reporting_args(raw_output={raw}) missing -nostdin"


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
