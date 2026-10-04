"""Composite grid video from a recording folder's mp4 files.

The layout is a 2D list of camera names (as defined in the config's
``[[cameras]]`` entries), where an empty string ``""`` means a black fill cell.
``octacam process`` builds one grid per ``[[visualization]]`` entry of the
recording's config, and none at all when the config has no such entry — the
composite is opt-in per rig, there is no built-in fallback layout.

Ask for one in your ``octacam_config.toml``:

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

from octacam.writer import (
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    ProgressCallback,
    _atomic_output,
    _color_range_args,
    _run_ffmpeg,
    _split_opts,
    find_ffmpeg,
    find_ffprobe,
)

log = logging.getLogger("octacam")

GRID_FILENAME = "grid.mp4"

# Bound on one ffprobe call. Probing reads only the container header, so a
# healthy file answers in milliseconds; a corrupt or network-backed one that
# hangs must not wedge `octacam process` with no diagnostic.
PROBE_TIMEOUT_S = 30.0


def auto_layout(camera_names: list[str]) -> list[list[str]]:
    """A near-square row-major layout for *camera_names*.

    Used by the ``octacam config`` scaffold to propose a starting layout from a
    rig's own cameras, which it then writes out as an explicit
    ``[[visualization]]`` entry.  The last row is padded with ``""`` (black)
    cells to keep every row the same length (required by ``xstack``).
    """
    names = [n for n in camera_names if n]
    if not names:
        return []
    cols = math.ceil(math.sqrt(len(names)))
    rows = math.ceil(len(names) / cols)
    padded = names + [""] * (rows * cols - len(names))
    return [padded[r * cols : (r + 1) * cols] for r in range(rows)]


def _fps_value(fps_str: str) -> float:
    """Convert a ``num/den`` fraction string (from ffprobe) to a float.

    Total for any input: ffprobe emits ``0/0`` for a stream with no defined
    frame rate (degenerate/zero-frame mp4), so a zero denominator yields 0.0
    instead of raising ZeroDivisionError."""
    num, _, den = fps_str.partition("/")
    if not den:
        return float(num)
    den_f = float(den)
    return float(num) / den_f if den_f else 0.0


def _probe_video(path: Path, ffprobe: str) -> tuple[int, int, str, float]:
    """Return (width, height, fps_fraction, duration_s) via ffprobe.

    ``stdin=DEVNULL`` keeps the probe off the controlling tty (the same rule
    every other ffmpeg-family launch here follows — a kill mid-probe must never
    leave the terminal in no-echo mode), and the timeout bounds a probe that
    hangs on a corrupt or network-backed file instead of wedging the run.
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
    fps = s["r_frame_rate"]  # e.g. "100/1"
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
    """Each cell's mp4 in row-major (xstack input) order, None for a black cell,
    and the first probed file's (width, height, fps, duration).

    A missing or unprobeable file becomes a black cell, so one bad camera never
    costs the whole grid.
    """
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

    Each cell is converted to *pix_fmt* before xstack: the camera videos are
    full range and the lavfi black cells limited range, and xstack's implicit
    conversion of mixed inputs mis-tags the range (washed out in VLC, a
    stalling stream in QuickTime). For limited-range YUV the scale keeps 0-255
    luma (``out_range=full``, tagged by writer._color_range_args), which also
    keeps the letterbox bars at true black.
    """
    scale_range = ":out_range=full" if _color_range_args(pix_fmt) else ""
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
    encoder, _ = _split_opts(
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
        *_color_range_args(pix_fmt),
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
    """Write a composite grid video to *output* (default: ``folder/grid.mp4``).

    *layout* is a 2D list of camera names / empty strings matching a
    ``[[visualization]]`` ``layout`` from the octacam config.  There is no
    default: an empty layout builds nothing (grids are opt-in per rig).

    *ffmpeg_params* supplies the encoder choice (``-c:v``/``-preset``/``-crf``);
    its ``-pix_fmt``/``-vf`` are ignored — the grid always outputs *pix_fmt*
    (yuv420p) for QuickTime / Keynote compatibility and owns its own filtergraph.
    Empty falls back to the default transcode encoder args.

    Missing cameras (name set but mp4 not found) are replaced with black frames
    so the grid is always produced even with a partial set.  Returns the output
    path on success, or None when no camera files are found or ffmpeg fails.

    Every cell is one uniform size, taken from the first present camera in
    row-major order.  Cameras whose native resolution / aspect ratio differs
    from that reference are letterboxed to fit (centred, with black bars) rather
    than stretched, so a rig with mixed frame sizes composites without
    distortion.

    On *dry_run* the ffmpeg command is logged but not executed; the intended
    output path is still returned so callers can include it in transfers.
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
        with _atomic_output(output) as tmp:
            _run_ffmpeg(
                [*cmd, str(tmp)],
                folder,
                on_progress=on_progress,
                total_frames=round(duration * _fps_value(fps)),
            )
    except RuntimeError as e:
        log.error("Grid generation failed: %s", e)
        return None
    return output
