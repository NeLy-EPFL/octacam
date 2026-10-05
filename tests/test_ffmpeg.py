"""ffmpeg toolchain: argument policy, off-tty launches and binary discovery."""

import functools
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from octacam import ffmpeg as ff
from octacam.ffmpeg import (
    color_range_args,
    find_ffmpeg,
    output_args,
    rawvideo_input_args,
    split_opts,
)
from octacam.transcode import transcode_file


def test_rawvideo_input_args_carry_the_geometry():
    args = rawvideo_input_args(64, 48, 30.0)
    assert args[args.index("-video_size") + 1] == "64x48"
    assert args[args.index("-framerate") + 1] == "30"
    assert args[args.index("-pixel_format") + 1] == "gray"
    assert args[args.index("-i") + 1] == "pipe:0"
    args = rawvideo_input_args(8, 6, 10.0, "in.raw", "gray")
    assert args[args.index("-i") + 1] == "in.raw"


def test_output_args_splice_params_verbatim():
    args = output_args("-c:v libx264 -preset ultrafast -crf 18 -pix_fmt gray", (64, 48))
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "ultrafast"


def test_split_opts_pulls_options_and_their_values():
    vf = ("-vf", "-filter:v")
    tokens = ["-c:v", "libx264", "-vf", "eq=contrast=2", "-crf", "18"]
    rest = ["-c:v", "libx264", "-crf", "18"]
    assert split_opts(tokens, vf) == (rest, ["eq=contrast=2"])
    both = ["-filter:v", "hflip", "-vf", "vflip"]
    assert split_opts(both, vf) == ([], ["hflip", "vflip"])
    assert split_opts(["-c:v", "libx264"], vf) == (["-c:v", "libx264"], [])
    # A trailing option with no value is dropped; flags go without a value.
    assert split_opts(["-y", "-vf"], vf) == (["-y"], [])
    flagged = ["-stats", "-i", "x"]
    assert split_opts(flagged, (), flags=("-stats",)) == (["-i", "x"], [])


def test_output_args_merge_user_vf():
    args = output_args("-c:v libx264 -vf eq=contrast=2 -pix_fmt gray", (64, 48))
    # ffmpeg takes one -vf: the user's filter, then the full-range conversion of
    # the yuv420p the gray output is written as.
    assert args.count("-vf") == 1
    assert args[args.index("-vf") + 1] == "eq=contrast=2,scale=out_range=full"


def test_color_range_args_only_for_limited_range_yuv():
    # YUV pixel formats default to limited/TV range (luma squeezed into 16-235);
    # we force full range so 0-255 frames survive. gray (4:0:0) and the yuvj*
    # aliases are already full range and need no flag.
    assert color_range_args("yuv420p") == ["-color_range", "pc"]
    assert color_range_args("yuv444p") == ["-color_range", "pc"]
    assert color_range_args("gray") == []
    assert color_range_args("yuvj420p") == []


def test_output_args_force_full_range_for_yuv_only():
    yuv = output_args("-c:v libx264 -crf 0 -preset ultrafast -pix_fmt yuv420p", (64, 48))
    assert yuv[yuv.index("-color_range") + 1] == "pc"
    # The full-range conversion filter is injected into the merged -vf.
    assert "scale=out_range=full" in yuv[yuv.index("-vf") + 1]

    # gray is already full range: no stray -color_range flag, no injected filter.
    # (An odd frame keeps libx264's gray; an even one is written as yuv420p.)
    gray = output_args("-c:v libx264 -crf 0 -preset ultrafast -pix_fmt gray", (63, 48))
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
    args = output_args(f"-c:v {encoder} -crf 18 -pix_fmt gray", (64, 48))
    assert _out_pix_fmt(args) == "yuv420p"
    assert args[args.index("-color_range") + 1] == "pc"
    assert "scale=out_range=full" in args[args.index("-vf") + 1]


@pytest.mark.parametrize(
    "spelling", ["-c:v", "-codec:v", "-vcodec", "-c:v:0", "-c", "-codec"]
)
def test_every_encoder_spelling_gets_the_4_2_0_rule(spelling):
    args = output_args(f"{spelling} libx264 -pix_fmt gray", (64, 48))
    assert _out_pix_fmt(args) == "yuv420p"


@pytest.mark.parametrize("size", [(63, 48), (64, 47)])
def test_odd_frames_stay_monochrome(size):
    # 4:2:0 cannot code an odd side (libx264: "width not divisible by 2"), so
    # gray is kept — and a yuv420p default falls back to it — rather than failing.
    for pix_fmt in ("gray", "yuv420p"):
        args = output_args(f"-c:v libx264 -pix_fmt {pix_fmt}", size)
        assert _out_pix_fmt(args) == "gray"
        assert "-color_range" not in args


def test_gray_is_left_alone_for_other_encoders_and_unknown_sizes():
    # FFV1 and friends decode gray in software everywhere: nothing to fix.
    assert _out_pix_fmt(output_args("-c:v ffv1 -pix_fmt gray", (64, 48))) == "gray"
    # With no known frame size an odd side cannot be ruled out.
    assert _out_pix_fmt(output_args("-c:v libx264 -pix_fmt gray")) == "gray"


def _decode_gray(path, width, height):
    dec = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(dec, dtype=np.uint8)[: width * height].reshape(height, width)


def test_gray_config_encodes_4_2_0_with_exact_luma(tmp_path):
    # End to end through a real ffmpeg: a lossless "-pix_fmt gray" encode is a
    # 4:2:0 stream (chroma_format_idc 1, which hardware decoders support) whose
    # luma decodes bit-exact.
    w, h = 256, 16
    ramp = np.tile(np.arange(256, dtype=np.uint8), (h, 1))
    raw = tmp_path / "ramp.raw"
    raw.write_bytes(ramp.tobytes())
    out = transcode_file(
        raw,
        tmp_path / "ramp.mkv",
        "-c:v libx264 -qp 0 -preset ultrafast -pix_fmt gray",
        width=w,
        height=h,
        fps=10.0,
    )
    headers = subprocess.run(
        [
            find_ffmpeg(),
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
    assert np.array_equal(_decode_gray(out, w, h), ramp)


def test_yuv420p_transcode_preserves_full_range(tmp_path):
    # Regression: a 0-255 ramp transcoded to yuv420p (lossless) must come back
    # spanning the full range, NOT clamped into limited range's 16-235. Decodes
    # straight to full-range gray and inspects the actual luma span.
    w, h = 256, 16
    ramp = np.tile(np.arange(256, dtype=np.uint8), (h, 1))
    raw = tmp_path / "ramp.raw"
    raw.write_bytes(ramp.tobytes())
    out = transcode_file(
        raw,
        tmp_path / "ramp.mp4",
        "-c:v libx264 -crf 0 -preset veryslow -pix_fmt yuv420p",
        width=w,
        height=h,
        fps=10.0,
    )
    back = _decode_gray(out, w, h)
    # Limited range would clamp to [16, 235]; full range reaches the extremes
    # (lossless crf 0, so this is effectively exact).
    assert back.min() <= 2 and back.max() >= 253, (int(back.min()), int(back.max()))


def test_ffmpeg_launches_never_grab_the_tty(monkeypatch):
    # An ffmpeg with the controlling terminal on stdin flips the tty to
    # no-echo/cbreak (to watch for keypresses) and only restores it on a clean
    # exit. `octacam doctor`'s NVENC probes launch encodes (12 concurrent ones
    # race the restore), and a timeout-killed encode never restores at all,
    # leaving the user's shell with invisible input. Every probe/transcode
    # launch must keep ffmpeg off the tty: -nostdin in the args AND
    # stdin=subprocess.DEVNULL. (The capture pipe is exempt: its stdin is the
    # frame pipe.)
    from octacam.transcode import run_ffmpeg

    def assert_off_tty(cmd, kwargs, what):
        assert "-nostdin" in cmd, f"{what}: missing -nostdin in {cmd}"
        assert kwargs.get("stdin") is subprocess.DEVNULL, (
            f"{what}: stdin not redirected to DEVNULL (kwargs={kwargs})"
        )

    runs: list[tuple[list, dict]] = []

    def fake_run(cmd, **kwargs):
        runs.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ff.subprocess, "run", fake_run)

    # 1. ffmpeg_encoder_works, through a fresh cache so the process's real
    #    probe results survive.
    probe = functools.cache(ff.ffmpeg_encoder_works.__wrapped__)
    monkeypatch.setattr(ff, "ffmpeg_encoder_works", probe)
    assert probe("/fake/ffmpeg", "h264_nvenc") is True
    # 2. The doctor's queries.
    ff.ffmpeg_version("/fake/ffmpeg")
    ff.ffmpeg_query("/fake/ffmpeg", "-hide_banner", "-encoders")
    assert len(runs) == 3
    for cmd, kwargs in runs:
        assert_off_tty(cmd, kwargs, cmd[-1])

    # 3. probe_nvenc_max_sessions: up to 12 concurrent encodes via Popen.
    monkeypatch.setattr(ff, "find_ffmpeg", lambda **_: "/fake/ffmpeg")
    launches: list[tuple[list, dict]] = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            launches.append((cmd, kwargs))
            self.stdout, self.stderr = io.StringIO(), io.StringIO()

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ff.subprocess, "Popen", FakeProc)
    assert ff.probe_nvenc_max_sessions(ceiling=12) == 12
    assert len(launches) == 12
    for cmd, kwargs in launches:
        assert_off_tty(cmd, kwargs, "probe_nvenc_max_sessions")

    # 4. Transcodes and grids (run_ffmpeg), in both progress modes; a caller's
    #    own -nostdin is not doubled.
    runs.clear()
    launches.clear()
    for raw in (False, True):
        argv = ["/fake/ffmpeg", "-nostdin", "-i", "in.mkv", "out.mp4"]
        run_ffmpeg(argv, Path("in.mkv"), raw_output=raw)
    assert len(runs) == 1 and len(launches) == 1
    for cmd, kwargs in [*runs, *launches]:
        assert_off_tty(cmd, kwargs, "run_ffmpeg")
        assert cmd.count("-nostdin") == 1, cmd


def test_ffmpeg_source_names_each_origin(tmp_path, monkeypatch):
    # The doctor labels the resolved ffmpeg by where find_ffmpeg's search found it.
    import imageio_ffmpeg

    bundled = tmp_path / "bundled-ffmpeg"
    on_path = tmp_path / "bin" / "ffmpeg"
    on_path.parent.mkdir()
    for exe in (bundled, on_path):
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(bundled))
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.delenv("OCTACAM_FFMPEG", raising=False)

    assert ff.find_ffmpeg() == str(bundled)
    assert ff.ffmpeg_source(str(bundled)).startswith("bundled imageio-ffmpeg")
    assert ff.ffmpeg_source(str(on_path)) == "system PATH"
    monkeypatch.setenv("OCTACAM_FFMPEG", str(on_path))
    assert ff.find_ffmpeg() == str(on_path)
    assert ff.ffmpeg_source(str(on_path)) == "OCTACAM_FFMPEG override"
