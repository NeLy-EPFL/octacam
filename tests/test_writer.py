"""Writer tests: ffmpeg/raw sinks, transcode roundtrip, failure paths."""

import time

import numpy as np
import pytest

from octacam.config import RecordConfig
from octacam.writer import (
    DEFAULT_CRF,
    FORMATS,
    AsyncFrameWriter,
    FfmpegVideoWriter,
    RawVideoWriter,
    _color_range_args,
    _merge_vf,
    build_encode_args,
    default_save_method,
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


def test_merge_vf_transform_first_then_user_filter():
    # The single -vf ffmpeg allows must merge the octacam transform with any
    # user -vf inside ffmpeg_params, transform first (rotate/flip) then user.
    tokens = ["-c:v", "libx264", "-vf", "eq=contrast=2", "-crf", "18"]
    cleaned, merged = _merge_vf("transpose=1", tokens)
    assert "-vf" not in cleaned
    assert merged == "transpose=1,eq=contrast=2"

    # -filter:v is treated the same as -vf.
    cleaned, merged = _merge_vf("", ["-filter:v", "hflip"])
    assert cleaned == []
    assert merged == "hflip"

    # No user filter: just the transform survives.
    _, merged = _merge_vf("vflip", ["-c:v", "libx264"])
    assert merged == "vflip"


def test_build_encode_args_merges_user_vf_after_transform():
    args = build_encode_args(
        "ffmpeg",
        "o.mkv",
        30.0,
        64,
        48,
        "-c:v libx264 -vf eq=contrast=2 -pix_fmt gray",
        vf="transpose=1",
    )
    assert args[args.index("-vf") + 1] == "transpose=1,eq=contrast=2"


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
    gray = build_encode_args(
        ffmpeg_params="-c:v libx264 -crf 0 -preset ultrafast -pix_fmt gray",
        **common,
    )
    assert "-color_range" not in gray
    assert "-vf" not in gray


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


def test_default_save_method_resolution():
    assert default_save_method(RecordConfig()) == "ffmpeg"  # default
    assert default_save_method(RecordConfig(save_method="raw")) == "raw"
    assert default_save_method(RecordConfig(save_method="ffmpeg")) == "ffmpeg"


def test_default_save_method_unknown_falls_back_to_ffmpeg():
    # A stray/unknown save_method must never stop a recording; it falls back to
    # ffmpeg. (RecordConfig's lenient validation coerces a bad Literal back to
    # the default, so exercise the resolver directly with a bare object too.)
    class _Bogus:
        save_method = "bogus"

    assert default_save_method(_Bogus()) == "ffmpeg"


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
