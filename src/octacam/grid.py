"""Composite grid videos from a recording folder's mp4 files.

``octacam process`` builds one grid per ``[[visualization]]`` entry of the
recording's config, and none without one (grids are opt-in per rig). A layout
is a 2D list of camera names, ``""`` for a black cell:

    [[visualization]]
    name = "grid.mp4"
    layout = [
        ["camera_LF", "",           "camera_RF"],
        ["camera_LM", "camera_F",   "camera_RM"],
        ["camera_LH", "camera_H",   "camera_RH"],
    ]
"""

from __future__ import annotations

import json
import logging
import math
import shlex
import subprocess
from pathlib import Path

from octacam.ffmpeg import (
    color_range_args,
    find_ffmpeg,
    find_ffprobe,
    is_limited_range_yuv,
    split_opts,
)
from octacam.transcode import (
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    ProgressCallback,
    atomic_output,
    run_ffmpeg,
)

log = logging.getLogger("octacam")

GRID_FILENAME = "grid.mp4"

# A probe reads only the container header; a corrupt or network-backed file
# that hangs it must not wedge `octacam process`.
PROBE_TIMEOUT_S = 30.0


def auto_layout(camera_names: list[str]) -> list[list[str]]:
    """A near-square row-major layout for *camera_names* (``octacam config``'s
    proposal), the last row padded with ``""``: xstack needs equal rows."""
    names = [n for n in camera_names if n]
    if not names:
        return []
    cols = math.ceil(math.sqrt(len(names)))
    rows = math.ceil(len(names) / cols)
    padded = names + [""] * (rows * cols - len(names))
    return [padded[r * cols : (r + 1) * cols] for r in range(rows)]


def _fps_value(fps_str: str) -> float:
    """ffprobe's ``num/den`` rate as a float; its ``0/0`` (no defined rate) is 0.0."""
    num, _, den = fps_str.partition("/")
    if not den:
        return float(num)
    den_f = float(den)
    return float(num) / den_f if den_f else 0.0


def _probe_video(path: Path, ffprobe: str) -> tuple[int, int, str, float]:
    """(width, height, fps fraction, duration s) of *path*'s video stream.

    Off the tty like every ffmpeg launch (see :mod:`octacam.ffmpeg`); ffprobe
    has no ``-nostdin``, so only ``stdin=DEVNULL``.
    """
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=PROBE_TIMEOUT_S,
    )
    data = json.loads(result.stdout)
    s = data["streams"][0]
    w, h = s["width"], s["height"]
    fps = s["r_frame_rate"]
    dur = float(data["format"]["duration"])
    return w, h, fps, dur


_PROBE_ERRORS = (
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
    KeyError,
    IndexError,
    ValueError,
)


def _probe_cells(
    folder: Path, layout: list[list[str]], ffprobe: str
) -> tuple[list[Path | None], tuple[int, int, str, float] | None]:
    """Each cell's mp4 in row-major (xstack input) order and the first probed
    file's (width, height, fps, duration). A missing or unprobeable file is a
    black cell (None), so one bad camera never costs the whole grid."""
    cells: list[Path | None] = []
    ref: tuple[int, int, str, float] | None = None
    for name in (name for row in layout for name in row):
        path = folder / f"{name}.mp4"
        if not name or not path.exists():
            cells.append(None)
            continue
        try:
            probe = _probe_video(path, ffprobe)
        except _PROBE_ERRORS as e:
            log.warning("Could not probe %s: %s — treating as a black cell", path, e)
            cells.append(None)
            continue
        cells.append(path)
        ref = ref or probe
    return cells, ref


def _filtergraph(rows: int, cols: int, width: int, height: int, pix_fmt: str) -> str:
    """Letterbox every input into a width×height cell, then xstack the cells.

    Each cell is converted to *pix_fmt* first: xstack's implicit conversion of
    full-range camera videos and limited-range lavfi cells mis-tags the range
    (washed out in VLC, stalling in QuickTime). ``out_range=full`` keeps 0-255
    luma for limited-range YUV (see ffmpeg.color_range_args), so the pad's bars
    are true black. ``force_divisible_by=2`` keeps a letterboxed camera's fitted
    size even, as yuv420p needs.
    """
    scale_range = ":out_range=full" if is_limited_range_yuv(pix_fmt) else ""
    n_cells = rows * cols
    parts = [
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2{scale_range},format={pix_fmt},"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[c{i}]"
        for i in range(n_cells)
    ]
    inputs = "".join(f"[c{i}]" for i in range(n_cells))
    positions = "|".join(
        f"{c * width}_{r * height}" for r in range(rows) for c in range(cols)
    )
    parts.append(f"{inputs}xstack=inputs={n_cells}:layout={positions}:shortest=1[grid]")
    return ";".join(parts)


def _grid_command(
    cells: list[Path | None],
    rows: int,
    cols: int,
    width: int,
    height: int,
    fps: str,
    ffmpeg_params: str,
    pix_fmt: str,
) -> list[str]:
    """The ffmpeg argv composing *cells*, all but the output path."""
    cmd = [find_ffmpeg(), "-y"]
    for path in cells:
        if path is None:
            # Outlasts any recording: xstack's shortest=1 ends at the first real video.
            source = f"color=black:size={width}x{height}:duration=86400:rate={fps}"
            cmd += ["-f", "lavfi", "-i", source]
        else:
            cmd += ["-i", str(path)]
    # The grid owns its pixel format and filters; ffmpeg_params picks the encoder.
    encoder, _ = split_opts(
        shlex.split(ffmpeg_params or DEFAULT_TRANSCODE_FFMPEG_PARAMS),
        ("-pix_fmt", "-pixel_format", "-vf", "-filter:v"),
    )
    return [
        *cmd,
        "-filter_complex",
        _filtergraph(rows, cols, width, height, pix_fmt),
        "-map",
        "[grid]",
        "-r",
        str(max(1, round(_fps_value(fps)))),  # QuickTime mishandles a fractional rate
        *encoder,
        "-pix_fmt",
        pix_fmt,
        *color_range_args(pix_fmt),
    ]


def build_grid_video(
    folder: Path,
    layout: list[list[str]],
    output: Path | None = None,
    ffmpeg_params: str = "",
    pix_fmt: str = "yuv420p",
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Path | None:
    """Write *layout*'s camera videos as one grid to *output* (default
    ``folder/grid.mp4``).

    *ffmpeg_params* picks the encoder (empty: the transcode default); its
    ``-pix_fmt``/``-vf`` are ignored, as the grid owns its filters and writes
    *pix_fmt* (yuv420p, for QuickTime/Keynote). Every cell takes the first
    probed camera's size; other sizes are letterboxed, a missing camera is
    black. Returns *output* (also on *dry_run*, which only logs the command),
    or None when nothing was built.
    """
    if not layout or not layout[0]:
        log.warning("Grid layout is empty — skipping grid")
        return None
    if output is None:
        output = folder / GRID_FILENAME
    # imageio-ffmpeg bundles no ffprobe: skip the grid with the real reason
    # rather than abort `octacam process` before its transfer.
    try:
        ffprobe = find_ffprobe()
    except RuntimeError as e:
        log.error("Cannot build the grid: %s", e)
        return None
    cells, ref = _probe_cells(folder, layout, ffprobe)
    if ref is None:
        log.warning(
            "No probeable mp4 files matching the grid layout found in %s — skipping grid",
            folder,
        )
        return None
    width, height, fps, duration = ref
    # yuv420p needs even dimensions, and some sensors size in 1-px steps.
    width += width & 1
    height += height & 1
    cmd = _grid_command(
        cells, len(layout), len(layout[0]), width, height, fps, ffmpeg_params, pix_fmt
    )
    if dry_run:
        log.info("[dry-run] grid: %s", " ".join([*cmd, str(output)]))
        return output

    log.info("Generating grid video → %s", output)
    try:
        # A partial grid.mp4 must never pass for a finished one.
        with atomic_output(output) as tmp:
            run_ffmpeg(
                [*cmd, str(tmp)],
                folder,
                on_progress=on_progress,
                total_frames=round(duration * _fps_value(fps)),
            )
    except RuntimeError as e:
        log.error("Grid generation failed: %s", e)
        return None
    return output
