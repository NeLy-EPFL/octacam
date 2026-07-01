"""Composite grid video from a recording folder's mp4 files.

The layout is a 2D list of camera names (as defined in the config's
``[[cameras]]`` entries), where an empty string ``""`` means a black fill cell.
``octacam process`` reads it from each ``[[visualization]]`` entry of the
recording's config; otherwise the built-in default below is used.

Default layout for the 7-camera 2p rig:

      col → 0 (left)    1 (centre)   2 (right)
  row ↓
    0        camera_LF    [black]      camera_RF
    1        camera_LM    camera_F     camera_RM
    2        camera_LH    [black]      camera_RH

Define a custom layout in your ``octacam_config.toml``:

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
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octacam.writer import ProgressCallback

log = logging.getLogger("octacam")

GRID_FILENAME = "grid.mp4"

# Built-in default: 3×3 grid for the standard 7-camera rig.
# Each cell is either a full camera name (stem of the mp4 file) or "" (black).
DEFAULT_LAYOUT: list[list[str]] = [
    ["camera_LF", "", "camera_RF"],
    ["camera_LM", "camera_F", "camera_RM"],
    ["camera_LH", "", "camera_RH"],
]


def auto_layout(camera_names: list[str]) -> list[list[str]]:
    """A near-square row-major layout for *camera_names*.

    Used when a config has cameras but no usable ``[grid] layout``, so the grid
    reflects this rig's actual cameras instead of the built-in 7-camera 2-photon
    :data:`DEFAULT_LAYOUT`.  The last row is padded with ``""`` (black) cells to
    keep every row the same length (required by ``xstack``).
    """
    names = [n for n in camera_names if n]
    if not names:
        return []
    cols = math.ceil(math.sqrt(len(names)))
    rows = math.ceil(len(names) / cols)
    padded = names + [""] * (rows * cols - len(names))
    return [padded[r * cols : (r + 1) * cols] for r in range(rows)]


def _fps_value(fps_str: str) -> float:
    """Convert a ``num/den`` fraction string (from ffprobe) to a float."""
    num, _, den = fps_str.partition("/")
    return float(num) / float(den) if den else float(num)


def _find_mp4(folder: Path, camera_name: str) -> Path | None:
    """Return ``folder / <camera_name>.mp4`` if it exists, else None."""
    p = folder / f"{camera_name}.mp4"
    return p if p.exists() else None


def _probe_video(path: Path) -> tuple[int, int, str, float]:
    """Return (width, height, fps_fraction, duration_s) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
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
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    s = data["streams"][0]
    w, h = s["width"], s["height"]
    fps = s["r_frame_rate"]  # e.g. "100/1"
    dur = float(data["format"]["duration"])
    return w, h, fps, dur


def build_grid_video(
    folder: Path,
    layout: list[list[str]] | None = None,
    output: Path | None = None,
    ffmpeg_params: str = "",
    pix_fmt: str = "yuv420p",
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Path | None:
    """Write a composite grid video to *output* (default: ``folder/grid.mp4``).

    *layout* is a 2D list of camera names / empty strings matching a
    ``[[visualization]]`` ``layout`` from the octacam config.  Omit to use the
    built-in 7-camera default.

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
    if layout is None:
        layout = DEFAULT_LAYOUT
    if not layout or not layout[0]:
        log.warning("Grid layout is empty — skipping grid")
        return None
    if output is None:
        output = folder / GRID_FILENAME

    rows = len(layout)
    cols = len(layout[0])
    n_cells = rows * cols

    # Resolve each grid slot to a source mp4 (None → black/missing).
    # Row-major order (left→right, top→bottom) matches xstack input order.
    slot_files: list[Path | None] = []
    found_any = False
    for row in layout:
        for cell in row:
            if not cell:  # empty string = explicit black fill
                slot_files.append(None)
            else:
                p = _find_mp4(folder, cell)
                slot_files.append(p)
                if p is not None:
                    found_any = True

    if not found_any:
        log.warning(
            "No mp4 files matching the grid layout found in %s — skipping grid", folder
        )
        return None

    # Probe the first real file for cell dimensions and fps.
    ref = next(p for p in slot_files if p is not None)
    try:
        W, H, fps, dur = _probe_video(ref)
    except (subprocess.CalledProcessError, KeyError, IndexError, ValueError) as e:
        log.warning("Could not probe %s: %s — skipping grid", ref, e)
        return None

    total_frames = round(dur * _fps_value(fps))

    # Build the ffmpeg command.
    # One -i per grid cell (real file or lavfi color source), in row-major order.
    # The black lavfi source uses a long duration; xstack's shortest=1 ends the
    # output when the first real video finishes.
    import shlex

    from octacam.writer import (
        DEFAULT_TRANSCODE_FFMPEG_PARAMS,
        _color_range_args,
        _run_ffmpeg,
        _strip_opts,
        find_ffmpeg,
    )

    cmd: list[str] = [find_ffmpeg(), "-y"]
    for p in slot_files:
        if p is not None:
            cmd += ["-i", str(p)]
        else:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                f"color=black:size={W}x{H}:duration=86400:rate={fps}",
            ]

    # filter_complex: scale each input to fit the cell (preserving its aspect
    # ratio, padding the remainder with black), then normalise to the output
    # pixel format *before* xstack.  Camera files are encoded as gray
    # (full-range, 0-255 luma) while lavfi black cells are yuv420p
    # (limited-range by default).  Without an explicit format= step xstack
    # receives mixed pixel formats and ffmpeg's implicit conversion mis-tags the
    # colour range, producing a washed-out image in VLC and a stalling bitstream
    # in QuickTime / Apple decoders.
    #
    # For limited-range YUV outputs we also pin the scale to full range
    # (out_range=full) so the gray→yuv conversion keeps the 0-255 luma instead
    # of squeezing it into 16-235; the matching -color_range pc on the output
    # (below) tags the stream so players expand it back.  See
    # writer._color_range_args.
    #
    # Cells whose native resolution / aspect ratio differs from the reference
    # W×H are letterboxed, not stretched: force_original_aspect_ratio=decrease
    # fits the frame inside the cell, force_divisible_by=2 keeps the fitted
    # dimensions even (required by chroma-subsampled outputs like yuv420p), and
    # pad centres it with black bars.  The pad respects the full-range tagging,
    # so the bars come out true black (luma 0) rather than washed-out 16.
    scale_range = ":out_range=full" if _color_range_args(pix_fmt) else ""
    filter_parts: list[str] = []
    labels: list[str] = []
    for i in range(n_cells):
        lbl = f"c{i}"
        labels.append(lbl)
        filter_parts.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2{scale_range},format={pix_fmt},"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[{lbl}]"
        )

    xstack_inputs = "".join(f"[{lbl}]" for lbl in labels)
    xstack_layout = "|".join(
        f"{c * W}_{r * H}" for r in range(rows) for c in range(cols)
    )
    filter_parts.append(
        f"{xstack_inputs}xstack=inputs={n_cells}:layout={xstack_layout}:shortest=1[grid]"
    )

    # Encoder args from the config ffmpeg_params, minus the pix_fmt/filter knobs
    # the grid owns itself (it always outputs pix_fmt via its own filtergraph).
    encoder = _strip_opts(
        shlex.split(ffmpeg_params or DEFAULT_TRANSCODE_FFMPEG_PARAMS),
        ("-pix_fmt", "-pixel_format", "-vf", "-filter:v"),
    )
    fps_int = max(1, round(_fps_value(fps)))
    cmd += [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[grid]",
        "-r",
        str(fps_int),  # pin integer fps — fractional fps confuses QuickTime
        *encoder,
        "-pix_fmt",
        pix_fmt,
        *_color_range_args(pix_fmt),
        str(output),
    ]

    if dry_run:
        log.info("[dry-run] grid: %s", " ".join(cmd))
        return output

    log.info("Generating grid video → %s", output)
    try:
        _run_ffmpeg(cmd, folder, on_progress=on_progress, total_frames=total_frames)
    except RuntimeError as e:
        log.error("Grid generation failed: %s", e)
        return None

    return output
