"""Grid compositor: full-range colour handling for YUV outputs.

The grid is the one path that defaults to a YUV pixel format (for browser /
QuickTime playback), so it must force full colour range or it loses the
0-255 → 16-235 squeeze on every cell. See writer._color_range_args.
"""

import logging
import os
import subprocess

os.environ.setdefault("PYLON_CAMEMU", "2")

import numpy as np
import pytest

from octacam.grid import build_grid_video
from octacam.writer import find_ffmpeg, transcode_raw

pytest.importorskip("cv2")  # parity with the other ffmpeg-backed suites

W, H = 64, 48


_GRAY_FFMPEG_PARAMS = "-c:v libx264 -preset ultrafast -crf 0 -pix_fmt gray"


def _gray_mp4(folder, name, frame=None):
    """Write a tiny gray (full-range) mp4 cell named ``<name>.mp4``."""
    if frame is None:
        frame = np.tile(np.arange(W, dtype=np.uint8) * (255 // (W - 1)), (H, 1))
    raw = folder / f"{name}.raw"
    raw.write_bytes(frame.astype(np.uint8).tobytes())
    out = folder / f"{name}.mp4"
    transcode_raw(
        raw,
        output=out,
        ffmpeg_params=_GRAY_FFMPEG_PARAMS,
        width=W,
        height=H,
        fps=10.0,
        pixel_format="Mono8",
    )
    return out


def _gray_mp4_sized(folder, name, w, h, value):
    """Write a solid-gray (full-range) mp4 of arbitrary ``w``×``h`` size.

    Used to build a rig with non-uniform frame sizes so the grid's
    letterboxing can be exercised (unlike :func:`_gray_mp4`, which is fixed at
    the module ``W``×``H``).
    """
    frame = np.full((h, w), value, dtype=np.uint8)
    raw = folder / f"{name}.raw"
    raw.write_bytes(frame.tobytes())
    out = folder / f"{name}.mp4"
    transcode_raw(
        raw,
        output=out,
        ffmpeg_params=_GRAY_FFMPEG_PARAMS,
        width=w,
        height=h,
        fps=10.0,
        pixel_format="Mono8",
    )
    return out


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _dry_run_cmd(folder, layout, pix_fmt):
    """Return the joined ffmpeg command build_grid_video would run."""
    handler = _ListHandler()
    logger = logging.getLogger("octacam")
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        build_grid_video(folder, layout=layout, pix_fmt=pix_fmt, dry_run=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    cmd = next((m for m in handler.messages if "[dry-run] grid:" in m), None)
    assert cmd is not None, handler.messages
    return cmd


def test_grid_yuv420p_forces_full_range(tmp_path):
    _gray_mp4(tmp_path, "a")
    _gray_mp4(tmp_path, "b")
    cmd = _dry_run_cmd(tmp_path, [["a", "b"]], "yuv420p")
    # Output stream is tagged full range, and the in-graph gray→yuv conversion
    # is pinned to full range so the luma is never squeezed into 16-235.
    assert "-color_range pc" in cmd
    assert "out_range=full" in cmd


def test_grid_gray_adds_no_range_flags(tmp_path):
    # gray (4:0:0) is already full range — no -color_range / out_range churn.
    _gray_mp4(tmp_path, "a")
    _gray_mp4(tmp_path, "b")
    cmd = _dry_run_cmd(tmp_path, [["a", "b"]], "gray")
    assert "-color_range" not in cmd
    assert "out_range" not in cmd


def test_grid_yuv420p_preserves_full_range_end_to_end(tmp_path):
    # End-to-end through the real filter graph (the version-sensitive path):
    # a 0..252 ramp cell must come back spanning the full range with the stream
    # tagged color_range=pc, NOT clamped into limited range's 16-235.
    ramp = np.tile(np.arange(W, dtype=np.uint8) * (255 // (W - 1)), (H, 1))
    _gray_mp4(tmp_path, "a", frame=ramp)
    out = build_grid_video(
        tmp_path,
        layout=[["a", ""]],
        ffmpeg_params="-c:v libx264 -preset veryslow -crf 20 -pix_fmt gray",
        pix_fmt="yuv420p",
    )
    assert out is not None and out.exists()

    tag = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=color_range",
            "-of",
            "default=nw=1:nk=1",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tag == "pc", tag

    # Crop the ramp cell back out and decode to full-range gray.
    dec = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-vf",
            f"crop={W}:{H}:0:0",
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    cell = np.frombuffer(dec, dtype=np.uint8)[: W * H].reshape(H, W)
    assert cell.min() <= 2 and cell.max() >= 250, (int(cell.min()), int(cell.max()))


def test_grid_letterboxes_non_uniform_sizes(tmp_path):
    # A rig with mismatched frame sizes must composite without distortion:
    # each mismatched cell is letterboxed (centred, black bars), never stretched
    # to fill.  Reference cell "a" is 64x48 (first present, row-major), so every
    # cell is 64x48.  "b" is wide (horizontal bars); "c" is tall (vertical bars).
    _gray_mp4_sized(tmp_path, "a", 64, 48, 100)  # reference size
    _gray_mp4_sized(tmp_path, "b", 128, 24, 255)  # wide white -> top/bottom bars
    _gray_mp4_sized(tmp_path, "c", 24, 128, 255)  # tall white -> left/right bars
    out = build_grid_video(
        tmp_path,
        layout=[["a", "b"], ["c", ""]],
        ffmpeg_params="-c:v libx264 -preset veryslow -crf 20 -pix_fmt gray",
        pix_fmt="yuv420p",
    )
    assert out is not None and out.exists()

    # 2x2 grid of uniform 64x48 cells -> 128x96.
    dims = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert dims == "128x96", dims

    def _cell(x, y):
        dec = subprocess.run(
            [
                find_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(out),
                "-vf",
                f"crop=64:48:{x}:{y}",
                "-f",
                "rawvideo",
                "-pixel_format",
                "gray",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return np.frombuffer(dec, dtype=np.uint8)[: 64 * 48].reshape(48, 64)

    # Wide cell "b" (top-right): black bars top & bottom (would be white edge to
    # edge if stretched), and at least one full-width white content row.
    b = _cell(64, 0)
    assert b[0].max() < 40 and b[-1].max() < 40, "wide cell stretched, not letterboxed"
    assert (b.min(axis=1) > 200).any(), "no full-width content row in wide cell"

    # Tall cell "c" (bottom-left): black bars left & right, and at least one
    # full-height white content column.
    c = _cell(0, 48)
    assert c[:, 0].max() < 40 and c[:, -1].max() < 40, (
        "tall cell stretched, not letterboxed"
    )
    assert (c.min(axis=0) > 200).any(), "no full-height content column in tall cell"

    # Reference cell "a" (top-left) fills its cell (no bars): mid-gray throughout.
    a = _cell(0, 0)
    assert a.min() > 60 and a.max() < 140, (int(a.min()), int(a.max()))


def test_fps_value_handles_degenerate_zero_denominator():
    # ffprobe emits "0/0" for a stream with no defined frame rate; it must not
    # raise ZeroDivisionError (it is called outside _probe_video's try/except).
    from octacam.grid import _fps_value

    assert _fps_value("100/1") == 100.0
    assert abs(_fps_value("30000/1001") - 29.97) < 0.01
    assert _fps_value("0/0") == 0.0
    assert _fps_value("30") == 30.0  # bare numerator fallback preserved


def test_grid_treats_unprobeable_file_as_black_cell(tmp_path):
    # A present-but-unprobeable mp4 must become a black cell, not abort the whole
    # grid, and the reference geometry/fps comes from the first file that probes.
    _gray_mp4(tmp_path, "a")
    (tmp_path / "b.mp4").write_bytes(b"not a real video")  # present but unprobeable
    cmd = _dry_run_cmd(tmp_path, [["a", "b"]], "yuv420p")
    # b is fed as a lavfi black source at the reference size, not as -i b.mp4.
    assert "b.mp4" not in cmd
    assert f"color=black:size={W}x{H}" in cmd


def test_grid_skips_only_when_no_file_probes(tmp_path):
    # If every present file is unprobeable, the grid is skipped (returns None)
    # rather than crashing.
    (tmp_path / "a.mp4").write_bytes(b"garbage")
    (tmp_path / "b.mp4").write_bytes(b"garbage")
    out = build_grid_video(tmp_path, layout=[["a", "b"]], dry_run=True)
    assert out is None


def test_grid_rounds_odd_reference_dims_up_to_even(tmp_path):
    # An odd-dimension source (gray tolerates odd; yuv420p does not) must be
    # rounded up to even so the composed yuv420p grid actually encodes.
    _gray_mp4_sized(tmp_path, "a", 63, 47, 120)  # odd width and height
    out = build_grid_video(
        tmp_path,
        # 1 row (odd) -> composed height == cell height; odd 47 would break
        # yuv420p unless rounded up to 48. Black second cell satisfies xstack's
        # 2-input minimum.
        layout=[["a", ""]],
        ffmpeg_params="-c:v libx264 -preset veryslow -crf 20 -pix_fmt gray",
        pix_fmt="yuv420p",
    )
    assert out is not None and out.exists()
    dims = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert dims == "128x48", dims  # 2 cols x (63->64), 1 row x (47->48)


def test_auto_layout_shapes():
    from octacam.grid import auto_layout

    assert auto_layout([]) == []
    assert auto_layout(["a"]) == [["a"]]
    assert auto_layout(["a", "b", "c", "d"]) == [["a", "b"], ["c", "d"]]
    # Empty cells aren't real cameras and don't count toward the geometry.
    assert auto_layout(["a", "", "b"]) == [["a", "b"]]

    seven = auto_layout([f"c{i}" for i in range(7)])
    assert len(seven) == 3 and all(len(row) == 3 for row in seven)  # near-square
    flat = [cell for row in seven for cell in row]
    assert flat[:7] == [f"c{i}" for i in range(7)]
    assert flat[7:] == ["", ""]  # last row padded with black cells
