"""Composite 3×3 grid video from a recording folder's mp4 files.

Layout for the standard 2p rig (LF/LM/LH on the left, RF/RM/RH on the
right, front camera centred in the middle row, remaining middle slots black):

      col → 0 (left)   1 (centre)  2 (right)
  row ↓
    0        LF          [black]      RF
    1        LM            F          RM
    2        LH          [black]      RH

Camera files are matched by the suffix after the last underscore in the stem
(e.g. ``camera_LF.mp4`` matches suffix ``LF``).  Missing cameras are filled
with a black frame so the grid is always produced even with a partial set.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("octacam")

GRID_FILENAME = "grid.mp4"

# None = black fill cell; str = camera-name suffix to look up.
_LAYOUT: list[list[str | None]] = [
    ["LF", None, "RF"],
    ["LM", "F",  "RM"],
    ["LH", None, "RH"],
]

_ROWS = len(_LAYOUT)
_COLS = len(_LAYOUT[0])
_N_CELLS = _ROWS * _COLS


def _find_mp4(folder: Path, suffix: str) -> Path | None:
    """First *.mp4 whose stem ends with ``_SUFFIX`` (case-insensitive)."""
    needle = f"_{suffix.upper()}"
    for p in sorted(folder.glob("*.mp4")):
        stem = p.stem.upper()
        if stem.endswith(needle) or stem == suffix.upper():
            return p
    return None


def _probe_video(path: Path) -> tuple[int, int, str, float]:
    """Return (width, height, fps_fraction, duration_s) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json",
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
    output: Path | None = None,
    crf: int = 20,
    preset: str = "veryslow",
    pix_fmt: str = "gray",
    dry_run: bool = False,
) -> Path | None:
    """Write a 3×3 composite video to *output* (default: ``folder/grid.mp4``).

    Missing cameras are replaced with black frames.  Returns the output path on
    success, or None when no camera files are found or ffmpeg fails.

    On *dry_run* the ffmpeg command is logged but not executed; the intended
    output path is still returned so callers can include it in NAS transfers.
    """
    if output is None:
        output = folder / GRID_FILENAME

    # Resolve each grid slot to a source mp4 (None → black/missing).
    # Row-major order (left→right, top→bottom) must match xstack input order.
    slot_files: list[Path | None] = []
    found_any = False
    for row in _LAYOUT:
        for suffix in row:
            if suffix is None:
                slot_files.append(None)
            else:
                p = _find_mp4(folder, suffix)
                slot_files.append(p)
                if p is not None:
                    found_any = True

    if not found_any:
        log.warning("No mp4 files matching the grid layout found in %s — skipping grid", folder)
        return None

    # Probe the first real file for cell dimensions and fps.
    ref = next(p for p in slot_files if p is not None)
    try:
        W, H, fps, _dur = _probe_video(ref)
    except (subprocess.CalledProcessError, KeyError, IndexError, ValueError) as e:
        log.warning("Could not probe %s: %s — skipping grid", ref, e)
        return None

    # Build the ffmpeg command.
    # One -i per grid cell (real file or lavfi color source), in row-major order.
    # The black lavfi source uses a large duration; xstack's shortest=1 will end
    # the output when the first real video finishes.
    cmd: list[str] = ["ffmpeg", "-y"]
    for p in slot_files:
        if p is not None:
            cmd += ["-i", str(p)]
        else:
            cmd += [
                "-f", "lavfi",
                "-i", f"color=black:size={W}x{H}:duration=86400:rate={fps}",
            ]

    # filter_complex: scale each input to the cell size → label, then xstack.
    filter_parts: list[str] = []
    labels: list[str] = []
    for i in range(_N_CELLS):
        lbl = f"c{i}"
        labels.append(lbl)
        filter_parts.append(f"[{i}:v]scale={W}:{H}[{lbl}]")

    xstack_inputs = "".join(f"[{l}]" for l in labels)
    xstack_layout = "|".join(
        f"{c * W}_{r * H}"
        for r in range(_ROWS)
        for c in range(_COLS)
    )
    filter_parts.append(
        f"{xstack_inputs}xstack=inputs={_N_CELLS}:layout={xstack_layout}:shortest=1[grid]"
    )

    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[grid]",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", pix_fmt,
        str(output),
    ]

    if dry_run:
        log.info("[dry-run] grid: %s", " ".join(cmd))
        return output

    log.info("Generating grid video → %s", output)
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        log.error(
            "Grid generation failed (ffmpeg exit %d):\n%s",
            e.returncode,
            e.stderr[-3000:],
        )
        return None

    return output
